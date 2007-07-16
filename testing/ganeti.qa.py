#!/usr/bin/python
#

# Copyright (C) 2006, 2007 Google Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301, USA.


"""Script for doing Q&A on Ganeti"""

import os
import re
import sys
import yaml
import time

from datetime import datetime
from optparse import OptionParser

# I want more flexibility for testing over SSH, therefore I'm not using
# Ganeti's ssh module.
import subprocess

from ganeti import utils
from ganeti import constants

# {{{ Global variables
cfg = None
options = None
# }}}

# {{{ Errors
class Error(Exception):
  """An error occurred during Q&A testing.

  """
  pass


class OutOfNodesError(Error):
  """Out of nodes.

  """
  pass


class OutOfInstancesError(Error):
  """Out of instances.

  """
  pass
# }}}

# {{{ Utilities
def TestEnabled(test):
  """Returns True if the given test is enabled."""
  return cfg.get('tests', {}).get(test, False)


def RunTest(callable, *args):
  """Runs a test after printing a header.

  """
  if callable.__doc__:
    desc = callable.__doc__.splitlines()[0].strip()
  else:
    desc = '%r' % callable

  now = str(datetime.now())

  print
  print '---', now, ('-' * (55 - len(now)))
  print desc
  print '-' * 60

  return callable(*args)


def AssertEqual(first, second, msg=None):
  """Raises an error when values aren't equal.

  """
  if not first == second:
    raise Error, (msg or '%r == %r' % (first, second))


def GetSSHCommand(node, cmd, strict=True):
  """Builds SSH command to be executed.

  """
  args = [ 'ssh', '-oEscapeChar=none', '-oBatchMode=yes', '-l', 'root' ]

  if strict:
    tmp = 'yes'
  else:
    tmp = 'no'
  args.append('-oStrictHostKeyChecking=%s' % tmp)
  args.append(node)

  if options.dry_run:
    prefix = 'exit 0; '
  else:
    prefix = ''

  args.append(prefix + cmd)

  if options.verbose:
    print 'SSH:', utils.ShellQuoteArgs(args)

  return args


def StartSSH(node, cmd, strict=True):
  """Starts SSH.

  """
  args = GetSSHCommand(node, cmd, strict=strict)
  return subprocess.Popen(args, shell=False)


def UploadFile(node, file):
  """Uploads a file to a node and returns the filename.

  Caller needs to remove the file when it's not needed anymore.
  """
  if os.stat(file).st_mode & 0100:
    mode = '0700'
  else:
    mode = '0600'

  cmd = ('tmp=$(tempfile --mode %s --prefix gnt) && '
         '[[ -f "${tmp}" ]] && '
         'cat > "${tmp}" && '
         'echo "${tmp}"') % mode

  f = open(file, 'r')
  try:
    p = subprocess.Popen(GetSSHCommand(node, cmd), shell=False, stdin=f,
                         stdout=subprocess.PIPE)
    AssertEqual(p.wait(), 0)

    name = p.stdout.read().strip()

    return name
  finally:
    f.close()
# }}}

# {{{ Config helpers
def GetMasterNode():
  return cfg['nodes'][0]


def AcquireInstance():
  """Returns an instance which isn't in use.

  """
  # Filter out unwanted instances
  tmp_flt = lambda inst: not inst.get('_used', False)
  instances = filter(tmp_flt, cfg['instances'])
  del tmp_flt

  if len(instances) == 0:
    raise OutOfInstancesError, ("No instances left")

  inst = instances[0]
  inst['_used'] = True
  return inst


def ReleaseInstance(inst):
  inst['_used'] = False


def AcquireNode(exclude=None):
  """Returns the least used node.

  """
  master = GetMasterNode()

  # Filter out unwanted nodes
  # TODO: Maybe combine filters
  if exclude is None:
    nodes = cfg['nodes'][:]
  else:
    nodes = filter(lambda node: node != exclude, cfg['nodes'])

  tmp_flt = lambda node: node.get('_added', False) or node == master
  nodes = filter(tmp_flt, nodes)
  del tmp_flt

  if len(nodes) == 0:
    raise OutOfNodesError, ("No nodes left")

  # Get node with least number of uses
  def compare(a, b):
    result = cmp(a.get('_count', 0), b.get('_count', 0))
    if result == 0:
      result = cmp(a['primary'], b['primary'])
    return result

  nodes.sort(cmp=compare)

  node = nodes[0]
  node['_count'] = node.get('_count', 0) + 1
  return node


def ReleaseNode(node):
  node['_count'] = node.get('_count', 0) - 1
# }}}

# {{{ Environment tests
def TestConfig():
  """Test configuration for sanity.

  """
  if len(cfg['nodes']) < 1:
    raise Error, ("Need at least one node")
  if len(cfg['instances']) < 1:
    raise Error, ("Need at least one instance")
  # TODO: Add more checks


def TestSshConnection():
  """Test SSH connection.

  """
  for node in cfg['nodes']:
    AssertEqual(StartSSH(node['primary'], 'exit').wait(), 0)


def TestGanetiCommands():
  """Test availibility of Ganeti commands.

  """
  cmds = ( ['gnt-cluster', '--version'],
           ['gnt-os', '--version'],
           ['gnt-node', '--version'],
           ['gnt-instance', '--version'],
           ['gnt-backup', '--version'],
           ['ganeti-noded', '--version'],
           ['ganeti-watcher', '--version'] )

  cmd = ' && '.join(map(utils.ShellQuoteArgs, cmds))

  for node in cfg['nodes']:
    AssertEqual(StartSSH(node['primary'], cmd).wait(), 0)


def TestIcmpPing():
  """ICMP ping each node.

  """
  for node in cfg['nodes']:
    check = []
    for i in cfg['nodes']:
      check.append(i['primary'])
      if i.has_key('secondary'):
        check.append(i['secondary'])

    ping = lambda ip: utils.ShellQuoteArgs(['ping', '-w', '3', '-c', '1', ip])
    cmd = ' && '.join(map(ping, check))

    AssertEqual(StartSSH(node['primary'], cmd).wait(), 0)
# }}}

# {{{ Cluster tests
def TestClusterInit():
  """gnt-cluster init"""
  master = GetMasterNode()

  cmd = ['gnt-cluster', 'init']
  if master.get('secondary', None):
    cmd.append('--secondary-ip=%s' % master['secondary'])
  cmd.append(cfg['name'])

  AssertEqual(StartSSH(master['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestClusterVerify():
  """gnt-cluster verify"""
  cmd = ['gnt-cluster', 'verify']
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestClusterInfo():
  """gnt-cluster info"""
  cmd = ['gnt-cluster', 'info']
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestClusterBurnin():
  """Burnin"""
  master = GetMasterNode()

  # Get as many instances as we need
  instances = []
  try:
    for _ in xrange(0, cfg.get('options', {}).get('burnin-instances', 1)):
      instances.append(AcquireInstance())
  except OutOfInstancesError:
    print "Not enough instances, continuing anyway."

  if len(instances) < 1:
    raise Error, ("Burnin needs at least one instance")

  # Run burnin
  try:
    script = UploadFile(master['primary'], '../tools/burnin')
    try:
      cmd = [script, '--os=%s' % cfg['os']]
      cmd += map(lambda inst: inst['name'], instances)
      AssertEqual(StartSSH(master['primary'],
                           utils.ShellQuoteArgs(cmd)).wait(), 0)
    finally:
      cmd = ['rm', '-f', script]
      AssertEqual(StartSSH(master['primary'],
                           utils.ShellQuoteArgs(cmd)).wait(), 0)
  finally:
    for inst in instances:
      ReleaseInstance(inst)


def TestClusterMasterFailover():
  """gnt-cluster masterfailover"""
  master = GetMasterNode()

  failovermaster = AcquireNode(exclude=master)
  try:
    cmd = ['gnt-cluster', 'masterfailover']
    AssertEqual(StartSSH(failovermaster['primary'],
                         utils.ShellQuoteArgs(cmd)).wait(), 0)

    cmd = ['gnt-cluster', 'masterfailover']
    AssertEqual(StartSSH(master['primary'],
                         utils.ShellQuoteArgs(cmd)).wait(), 0)
  finally:
    ReleaseNode(failovermaster)


def TestClusterDestroy():
  """gnt-cluster destroy"""
  cmd = ['gnt-cluster', 'destroy', '--yes-do-it']
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)
# }}}

# {{{ Node tests
def _NodeAdd(node):
  if node.get('_added', False):
    raise Error, ("Node %s already in cluster" % node['primary'])

  cmd = ['gnt-node', 'add']
  if node.get('secondary', None):
    cmd.append('--secondary-ip=%s' % node['secondary'])
  cmd.append(node['primary'])
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)

  node['_added'] = True


def TestNodeAddAll():
  """Adding all nodes to cluster."""
  master = GetMasterNode()
  for node in cfg['nodes']:
    if node != master:
      _NodeAdd(node)


def _NodeRemove(node):
  cmd = ['gnt-node', 'remove', node['primary']]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)
  node['_added'] = False


def TestNodeRemoveAll():
  """Removing all nodes from cluster."""
  master = GetMasterNode()
  for node in cfg['nodes']:
    if node != master:
      _NodeRemove(node)
# }}}

# {{{ Instance tests
def _DiskTest(node, instance, args):
  cmd = ['gnt-instance', 'add',
         '--os-type=%s' % cfg['os'],
         '--os-size=%s' % cfg['os-size'],
         '--swap-size=%s' % cfg['swap-size'],
         '--memory=%s' % cfg['mem'],
         '--node=%s' % node['primary']]
  if args:
    cmd += args
  cmd.append(instance['name'])

  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)
  return instance


def TestInstanceAddWithPlainDisk(node):
  """gnt-instance add -t plain"""
  return _DiskTest(node, AcquireInstance(), ['--disk-template=plain'])


def TestInstanceAddWithLocalMirrorDisk(node):
  """gnt-instance add -t local_raid1"""
  return _DiskTest(node, AcquireInstance(), ['--disk-template=local_raid1'])


def TestInstanceAddWithRemoteRaidDisk(node, node2):
  """gnt-instance add -t remote_raid1"""
  return _DiskTest(node, AcquireInstance(),
                   ['--disk-template=remote_raid1',
                    '--secondary-node=%s' % node2['primary']])


def TestInstanceRemove(instance):
  """gnt-instance remove"""
  cmd = ['gnt-instance', 'remove', '-f', instance['name']]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)

  ReleaseInstance(instance)


def TestInstanceStartup(instance):
  """gnt-instance startup"""
  cmd = ['gnt-instance', 'startup', instance['name']]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestInstanceShutdown(instance):
  """gnt-instance shutdown"""
  cmd = ['gnt-instance', 'shutdown', instance['name']]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestInstanceFailover(instance):
  """gnt-instance failover"""
  cmd = ['gnt-instance', 'failover', '--force', instance['name']]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)
# }}}

# {{{ Daemon tests
def _ResolveInstanceName(instance):
  """Gets the full Xen name of an instance.

  """
  master = GetMasterNode()

  info_cmd = utils.ShellQuoteArgs(['gnt-instance', 'info', instance['name']])
  sed_cmd = utils.ShellQuoteArgs(['sed', '-n', '-e', 's/^Instance name: *//p'])

  cmd = '%s | %s' % (info_cmd, sed_cmd)
  p = subprocess.Popen(GetSSHCommand(master['primary'], cmd), shell=False,
                       stdout=subprocess.PIPE)
  AssertEqual(p.wait(), 0)

  return p.stdout.read().strip()


def _InstanceRunning(node, name):
  """Checks whether an instance is running.

  Args:
    node: Node the instance runs on
    name: Full name of Xen instance
  """
  cmd = utils.ShellQuoteArgs(['xm', 'list', name]) + ' >/dev/null'
  ret = StartSSH(node['primary'], cmd).wait()
  return ret == 0


def _XmShutdownInstance(node, name):
  """Shuts down instance using "xm" and waits for completion.

  Args:
    node: Node the instance runs on
    name: Full name of Xen instance
  """
  cmd = ['xm', 'shutdown', name]
  AssertEqual(StartSSH(GetMasterNode()['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)

  # Wait up to a minute
  end = time.time() + 60
  while time.time() <= end:
    if not _InstanceRunning(node, name):
      break
    time.sleep(5)
  else:
    raise Error, ("xm shutdown failed")


def _ResetWatcherDaemon(node):
  """Removes the watcher daemon's state file.

  Args:
    node: Node to be reset
  """
  cmd = ['rm', '-f', constants.WATCHER_STATEFILE]
  AssertEqual(StartSSH(node['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestInstanceAutomaticRestart(node, instance):
  """Test automatic restart of instance by ganeti-watcher.

  Note: takes up to 6 minutes to complete.
  """
  master = GetMasterNode()
  inst_name = _ResolveInstanceName(instance)

  _ResetWatcherDaemon(node)
  _XmShutdownInstance(node, inst_name)

  # Give it a bit more than five minutes to start again
  restart_at = time.time() + 330

  # Wait until it's running again
  while time.time() <= restart_at:
    if _InstanceRunning(node, inst_name):
      break
    time.sleep(15)
  else:
    raise Error, ("Daemon didn't restart instance in time")

  cmd = ['gnt-instance', 'info', inst_name]
  AssertEqual(StartSSH(master['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)


def TestInstanceConsecutiveFailures(node, instance):
  """Test five consecutive instance failures.

  Note: takes at least 35 minutes to complete.
  """
  master = GetMasterNode()
  inst_name = _ResolveInstanceName(instance)

  _ResetWatcherDaemon(node)
  _XmShutdownInstance(node, inst_name)

  # Do shutdowns for 30 minutes
  finished_at = time.time() + (35 * 60)

  while time.time() <= finished_at:
    if _InstanceRunning(node, inst_name):
      _XmShutdownInstance(node, inst_name)
    time.sleep(30)

  # Check for some time whether the instance doesn't start again
  check_until = time.time() + 330
  while time.time() <= check_until:
    if _InstanceRunning(node, inst_name):
      raise Error, ("Instance started when it shouldn't")
    time.sleep(30)

  cmd = ['gnt-instance', 'info', inst_name]
  AssertEqual(StartSSH(master['primary'],
                       utils.ShellQuoteArgs(cmd)).wait(), 0)
# }}}

# {{{ Main program
if __name__ == '__main__':
  # {{{ Option parsing
  parser = OptionParser(usage="%prog [options] <configfile>")
  parser.add_option('--cleanup', dest='cleanup',
      action="store_true",
      help="Clean up cluster after testing?")
  parser.add_option('--dry-run', dest='dry_run',
      action="store_true",
      help="Show what would be done")
  parser.add_option('--verbose', dest='verbose',
      action="store_true",
      help="Verbose output")
  parser.add_option('--yes-do-it', dest='yes_do_it',
      action="store_true",
      help="Really execute the tests")
  (options, args) = parser.parse_args()
  # }}}

  if len(args) == 1:
    config_file = args[0]
  else:
    raise SyntaxError, ("Exactly one configuration file is expected")

  if not options.yes_do_it:
    print ("Executing this script irreversibly destroys any Ganeti\n"
           "configuration on all nodes involved. If you really want\n"
           "to start testing, supply the --yes-do-it option.")
    sys.exit(1)

  f = open(config_file, 'r')
  try:
    cfg = yaml.load(f.read())
  finally:
    f.close()

  RunTest(TestConfig)

  if TestEnabled('env'):
    RunTest(TestSshConnection)
    RunTest(TestIcmpPing)
    RunTest(TestGanetiCommands)

  RunTest(TestClusterInit)

  if TestEnabled('cluster-verify'):
    RunTest(TestClusterVerify)
    RunTest(TestClusterInfo)

  RunTest(TestNodeAddAll)

  if TestEnabled('cluster-burnin'):
    RunTest(TestClusterBurnin)

  if TestEnabled('cluster-master-failover'):
    RunTest(TestClusterMasterFailover)

  node = AcquireNode()
  try:
    if TestEnabled('instance-add-plain-disk'):
      instance = RunTest(TestInstanceAddWithPlainDisk, node)
      RunTest(TestInstanceShutdown, instance)
      RunTest(TestInstanceStartup, instance)

      if TestEnabled('instance-automatic-restart'):
        RunTest(TestInstanceAutomaticRestart, node, instance)

      if TestEnabled('instance-consecutive-failures'):
        RunTest(TestInstanceConsecutiveFailures, node, instance)

      RunTest(TestInstanceRemove, instance)
      del instance

    if TestEnabled('instance-add-local-mirror-disk'):
      instance = RunTest(TestInstanceAddWithLocalMirrorDisk, node)
      RunTest(TestInstanceShutdown, instance)
      RunTest(TestInstanceStartup, instance)
      RunTest(TestInstanceRemove, instance)
      del instance

    if TestEnabled('instance-add-remote-raid-disk'):
      node2 = AcquireNode(exclude=node)
      try:
        instance = RunTest(TestInstanceAddWithRemoteRaidDisk, node, node2)
        RunTest(TestInstanceShutdown, instance)
        RunTest(TestInstanceStartup, instance)

        if TestEnabled('instance-failover'):
          RunTest(TestInstanceFailover, instance)

        RunTest(TestInstanceRemove, instance)
        del instance
      finally:
        ReleaseNode(node2)

  finally:
    ReleaseNode(node)

  RunTest(TestNodeRemoveAll)

  if TestEnabled('cluster-destroy'):
    RunTest(TestClusterDestroy)
# }}}

# vim: foldmethod=marker :
