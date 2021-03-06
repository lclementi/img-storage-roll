#!/opt/rocks/bin/python
#
# @Copyright@
#
#                               Rocks(r)
#                        www.rocksclusters.org
#                        version 5.6 (Emerald Boa)
#                        version 6.1 (Emerald Boa)
#
# Copyright (c) 2000 - 2013 The Regents of the University of California.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
# 1. Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright
# notice unmodified and in its entirety, this list of conditions and the
# following disclaimer in the documentation and/or other materials provided
# with the distribution.
#
# 3. All advertising and press materials, printed or electronic, mentioning
# features or use of this software must display the following acknowledgement:
#
#       "This product includes software developed by the Rocks(r)
#       Cluster Group at the San Diego Supercomputer Center at the
#       University of California, San Diego and its contributors."
#
# 4. Except as permitted for the purposes of acknowledgment in paragraph 3,
# neither the name or logo of this software nor the names of its
# authors may be used to endorse or promote products derived from this
# software without specific prior written permission.  The name of the
# software includes the following terms, and any derivatives thereof:
# "Rocks", "Rocks Clusters", and "Avalanche Installer".  For licensing of
# the associated name, interested parties should contact Technology
# Transfer & Intellectual Property Services, University of California,
# San Diego, 9500 Gilman Drive, Mail Code 0910, La Jolla, CA 92093-0910,
# Ph: (858) 534-5815, FAX: (858) 534-7345, E-MAIL:invent@ucsd.edu
#
# THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS''
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
# THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS
# BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR
# BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE
# OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN
# IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#
# @Copyright@
#
from rabbitmqclient import RabbitMQCommonClient, RabbitMQLocator
from imgstorage import runCommand, ActionError, ZvolBusyActionError
import logging

import traceback

import time
import json

from pysqlite2 import dbapi2 as sqlite3
import sys
import signal
import pika
import socket
import rocks.db.helper

class NasDaemon():
    def __init__(self):
        self.stdin_path = '/dev/null'
        self.stdout_path = '/tmp/out.log'
        self.stderr_path = '/tmp/err.log'
        self.pidfile_path =  '/var/run/img-storage-nas.pid'
        self.pidfile_timeout = 5
        self.function_dict = {'set_zvol':self.set_zvol, 'tear_down':self.tear_down, 'zvol_attached':self.zvol_attached, 'zvol_detached': self.zvol_detached, 'list_zvols': self.list_zvols, 'del_zvol': self.del_zvol }

        self.ZPOOL = 'tank'
        self.SQLITE_DB = '/opt/rocks/var/img_storage.db'
        self.NODE_NAME = RabbitMQLocator().NODE_NAME

        self.logger = logging.getLogger(__name__)

        db = rocks.db.helper.DatabaseHelper()
        db.connect()
        self.ib_net = db.getHostAttr(db.getHostname(), 'IB_net')
        db.close()

    def run(self):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('CREATE TABLE IF NOT EXISTS zvol_calls(zvol TEXT PRIMARY KEY NOT NULL, reply_to TEXT NOT NULL, time INT NOT NULL)')
            cur.execute('CREATE TABLE IF NOT EXISTS zvols(zvol TEXT PRIMARY KEY NOT NULL, iscsi_target TEXT UNIQUE, hosting TEXT)')
            con.commit()

        self.queue_connector = RabbitMQCommonClient('rocks.vm-manage', 'direct', self.process_message)
        self.queue_connector.run()

    def failAction(self, routing_key, action, error_message):
        if(routing_key != None and action != None):
            self.queue_connector.publish_message({'action': action, 'status': 'error', 'error':error_message}, exchange='', routing_key=routing_key)
        self.logger.error("Failed %s: %s"%(action, error_message))

    """
    Received set_zvol command from frontend, passing to compute node
    """
    def set_zvol(self, message, props):
        hosting = message['hosting']
        zvol_name = message['zvol']
        self.logger.debug("Setting zvol %s"%zvol_name)

        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            try :
                self.lock_zvol(zvol_name, props.reply_to)

                cur.execute('SELECT count(*) FROM zvols WHERE zvol = ?',[zvol_name])
                if(cur.fetchone()[0] == 0):
                    runCommand(['zfs', 'create', '-o', 'primarycache=metadata', '-o', 'volblocksize=128K', '-V', '%sgb'%message['size'], '%s/%s'%(self.ZPOOL, zvol_name)])
                    cur.execute('INSERT OR REPLACE INTO zvols VALUES (?,?,?) ',(zvol_name, None, None))
                    con.commit()
                    self.logger.debug('Created new zvol %s'%zvol_name)

                cur.execute('SELECT iscsi_target FROM zvols WHERE zvol = ?',[zvol_name])
                row = cur.fetchone()
                if (row != None and row[0] != None): #zvol is mapped
                    raise ActionError('Error when mapping zvol: already mapped')

                ip = None
                use_ib = False

                if(self.ib_net):
                    try:
                        ip = socket.gethostbyname('%s.%s'%(hosting, self.ib_net))
                        use_ib = True
                    except:
                        pass

                if not use_ib:
                    try:
                        ip = socket.gethostbyname(hosting)
                    except:
                        raise ActionError('Host %s is unknown'%hosting)

                iscsi_target = ''

                out = runCommand(['tgt-setup-lun', '-n', zvol_name, '-d', '/dev/%s/%s'%(self.ZPOOL, zvol_name), ip])
                for line in out:
                    if "Creating new target" in line:
                        iscsi_target = line['Creating new target (name='.__len__():line.index(',')]
                self.logger.debug('Mapped %s to iscsi target %s'%(zvol_name, iscsi_target))

                cur.execute('INSERT OR REPLACE INTO zvols VALUES (?,?,?) ',(zvol_name, iscsi_target,hosting))
                con.commit()

                def failDeliver(target, zvol, reply_to, hosting):
                    self.detach_target(target)
                    self.failAction(props.reply_to, 'zvol_attached', 'Compute node %s is unavailable'%hosting)
                    self.release_zvol(zvol_name)

                self.queue_connector.publish_message(
                        {'action': 'set_zvol', 'target':iscsi_target, 'nas': ('%s.%s'%(self.NODE_NAME, self.ib_net)) if use_ib else self.NODE_NAME},
                        hosting,
                        self.NODE_NAME,
                        on_fail=lambda: failDeliver(iscsi_target, zvol_name, props.reply_to, hosting))
                self.logger.debug("Setting iscsi %s sent"%iscsi_target)
            except ActionError, err:
                if not isinstance(err, ZvolBusyActionError): self.release_zvol(zvol_name)
                self.failAction(props.reply_to, 'zvol_attached', str(err))

    """
    Received zvol tear_down command from frontend, passing to compute node
    """
    def tear_down(self, message, props):
        zvol_name = message['zvol']
        self.logger.debug("Tearing down zvol %s"%zvol_name)
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            try :
                cur.execute('SELECT hosting, iscsi_target FROM zvols WHERE zvol = ?',[zvol_name])
                row = cur.fetchone()
                if row == None: raise ActionError('ZVol %s not found in database'%zvol_name)
                if row[1] == None: raise ActionError('ZVol %s is not attached'%zvol_name)
                if self.find_iscsi_target_num(row[1]) == None: raise ActionError('iSCSI target does not exist for ZVol %s'%zvol_name)

                self.lock_zvol(zvol_name, props.reply_to)
                self.queue_connector.publish_message(
                        {'action': 'tear_down', 'target':row[1]},
                        row[0],
                        self.NODE_NAME,
                        on_fail=lambda: self.failAction(props.reply_to, 'zvol_detached', 'Compute node %s is unavailable'%row[0])
                )
                self.logger.debug("Tearing down zvol %s sent"%zvol_name)

            except ActionError, err:
                if not isinstance(err, ZvolBusyActionError): self.release_zvol(zvol_name)
                self.failAction(props.reply_to, 'zvol_detached', str(err))

    """
    Received zvol delete command from frontend
    """
    def del_zvol(self, message, props):
        zvol_name = message['zvol']
        self.logger.debug("Deleting zvol %s"%zvol_name)
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            try :
                self.lock_zvol(zvol_name, props.reply_to)
                cur.execute('SELECT hosting, iscsi_target FROM zvols WHERE zvol = ?',[zvol_name])
                row = cur.fetchone()
                if row == None: raise ActionError('ZVol %s not found in database'%zvol_name)
                if row[1] != None: raise ActionError('Error deleting zvol %s: is mapped'%zvol_name)

                self.logger.debug("Invoking zfs destroy %s/%s"%(self.ZPOOL,zvol_name))
                runCommand(['zfs', 'destroy', '%s/%s'%(self.ZPOOL, zvol_name)])
                self.logger.debug('zfs destroy success %s'%zvol_name)

                cur.execute('DELETE FROM zvols WHERE zvol = ?',[zvol_name])
                con.commit()

                self.release_zvol(zvol_name)
                self.queue_connector.publish_message({'action': 'zvol_deleted', 'status': 'success'}, exchange='', routing_key=props.reply_to)
            except ActionError, err:
                if not isinstance(err, ZvolBusyActionError): self.release_zvol(zvol_name)
                self.failAction(props.reply_to, 'zvol_deleted', str(err))

    """
    Received zvol_attached notification from compute node, passing to frontend
    """
    def zvol_attached(self, message, props):
        target = message['target']

        zvol = None
        reply_to = None

        self.logger.debug("Got zvol attached message %s"%target)
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('SELECT reply_to, zvol_calls.zvol FROM zvol_calls JOIN zvols ON zvol_calls.zvol = zvols.zvol WHERE zvols.iscsi_target = ?',[target])
            reply_to, zvol = cur.fetchone()

            self.release_zvol(zvol)
            if(message['status'] == 'success'):
                self.queue_connector.publish_message({'action': 'zvol_attached', 'bdev':message['bdev'], 'status': 'success'}, exchange='', routing_key=reply_to)
            else:
                self.failAction(reply_to, 'zvol_attached', message.get('error'))

    """
    Received zvol_detached notification from compute node, passing to frontend
    """
    def zvol_detached(self, message, props):
        target = message['target']
        self.logger.debug("Got zvol %s detached message"%(target))

        zvol = None
        reply_to = None

        try:
            with sqlite3.connect(self.SQLITE_DB) as con:
                cur = con.cursor()

                # get request destination
                cur.execute('SELECT reply_to, zvol_calls.zvol FROM zvol_calls JOIN zvols ON zvol_calls.zvol = zvols.zvol WHERE zvols.iscsi_target = ?',[target])
                reply_to, zvol = cur.fetchone()

                if(message['status'] == 'error'):
                    raise ActionError('Error detaching iSCSI target from compute node: %s'%message.get('error'))

                self.detach_target(target)

                self.release_zvol(zvol)
                self.queue_connector.publish_message({'action': 'zvol_detached', 'status': 'success'}, exchange='', routing_key=reply_to)

        except ActionError, err:
            self.release_zvol(zvol)
            self.failAction(caller_properties['reply_to'], 'zvol_detached', str(err))

    def detach_target(self, target):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()

            tgt_num = self.find_iscsi_target_num(target)
            runCommand(['tgtadm', '--lld', 'iscsi', '--op', 'delete', '--mode', 'target', '--tid', tgt_num])# remove iscsi target

            cur.execute('UPDATE zvols SET iscsi_target = NULL where iscsi_target = ?',[target])
            con.commit()

    def find_iscsi_target_num(self, target):
        out = runCommand(['tgtadm', '--op', 'show', '--mode', 'target'])
        self.logger.debug("Looking for target's number in output of tgtadm")
        for line in out:
            if line.startswith('Target ') and line.split()[2] == target:
                tgt_num = line.split()[1][:-1]
                self.logger.debug("target number is %s"%tgt_num)
                return tgt_num
        return None

    def list_zvols(self, message, properties):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('SELECT * from zvols')
            r = [dict((cur.description[i][0], value) for i, value in enumerate(row)) for row in cur.fetchall()]
            self.queue_connector.publish_message({'action': 'zvol_list', 'status': 'success', 'body':r}, exchange='', routing_key=properties.reply_to)


    def process_message(self, properties, message):
        self.logger.debug("Received message %s"%message)
        if message['action'] not in self.function_dict.keys():
            self.queue_connector.publish_message({'status': 'error', 'error':'action_unsupported'}, exchange='', routing_key=properties.reply_to)
            return

        try:
            self.function_dict[message['action']](message, properties)
        except:
            self.logger.error("Unexpected error: %s %s"%(sys.exc_info()[0], sys.exc_info()[1]))
            traceback.print_tb(sys.exc_info()[2])
            self.queue_connector.publish_message({'status': 'error', 'error':sys.exc_info()[1].message}, exchange='', routing_key=properties.reply_to)

    def stop(self):
        self.queue_connector.stop()
        self.logger.info('RabbitMQ connector stopping called')

    def lock_zvol(self, zvol_name, reply_to):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            try:
                cur.execute('INSERT INTO zvol_calls VALUES (?,?,?)',(zvol_name, reply_to, time.time()))
                con.commit()
            except sqlite3.IntegrityError:
                raise ZvolBusyActionError('ZVol %s is busy'%zvol_name)

    def release_zvol(self, zvol):
        with sqlite3.connect(self.SQLITE_DB) as con:
            cur = con.cursor()
            cur.execute('DELETE FROM zvol_calls WHERE zvol = ?',[zvol])
            con.commit()
