import types
import os
import shutil
import re
import tempfile
from teuthology import misc
from teuthology.orchestra.run import PIPE
from StringIO import StringIO
from types import *
import logging
import socket
import subprocess
import Queue
from orchestra.opsys import OS
from threading import Thread, Condition, Lock

log = logging.getLogger(__name__)

class Raw:
    def __init__(self, value):
        self.value = value

class Commands:
    def __init__(self):
        self.queue = Queue.Queue()
        self.thread = Thread(target=self.run)
        self.thread.start()

    def add(self, container, original_args, kwargs):
        command = Command(container, original_args, kwargs)
        self.queue.put(command)
        if kwargs.get('wait', True):
            command.wait()
        return command

    def run(self):
        while True:
            action = self.queue.get()
            action.spawn()
            self.queue.task_done()

class Command:
    def __init__(self, container, original_args, kwargs):
        self.lock = Lock()
        self.lock.acquire()
        self.finished = False
        self.container = container
        self.original_args = original_args
        self.kwargs = kwargs
        stdin = kwargs.get('stdin', None)
        if stdin == PIPE:
            (stdin_r, stdin_w) = os.pipe()
            self.stdin = os.fdopen(stdin_w, 'w', 0)
            self.stdin_r = os.fdopen(stdin_r, 'r', 0)
        elif stdin != None:
            self.stdin_r = stdin
        else:
            self.stdin_r = None
        self.args = ['sudo', 'docker', 'exec', '-i', self.container.name] + kwargs['args']
        log.info("command " + self.container.name + " " + self.original_args)

    def file_copy(self, f, t):
        if not t or f.closed:
            return False
        buf = f.read(4096)
        if not buf:
            f.close()
            return False
        log.debug("command " + self.container.name + " " + self.original_args + ": " + buf)
        t.write(buf)
        return True

    def spawn(self):
        self.stdout = self.kwargs.get('stdout')
        stdout = subprocess.PIPE if self.stdout else None
        self.stderr = self.kwargs.get('stderr')
        stderr = subprocess.PIPE if self.stderr else None
        self.p = subprocess.Popen(self.args,
                                  stdin=self.stdin_r,
                                  stdout=stdout, stderr=stderr,
                                  close_fds=True,)
        del self.stdin_r
        while ( self.file_copy(self.p.stdout, self.stdout) or
                self.file_copy(self.p.stderr, self.stderr) ):
            pass
        log.info("consumed stderr and stdout on %s: %s" %
                 (self.container.name, self.original_args))
        self.finished = True
        self.lock.release()

    def wait(self):
        self.lock.acquire()
        log.info("waiting on %s: %s" %
                 (self.container.name, self.original_args))
        self.p.wait()
        log.info("completed on %s: %s" %
                 (self.container.name, self.original_args))
        self.exitstatus = self.p.returncode
        self.lock.release()

class Container:
    class SSH:
        def __init__(self, name):
            self.name = name

        def get_transport(self):
            class Transport:
                def __init__(self, name):
                    self.name = name

                def getpeername(self):
                    ip = subprocess.check_output(['sudo', 'docker', 'inspect',  '-f', '{{ .NetworkSettings.IPAddress }}', self.name])
                    return (ip.strip(), None)

            return Transport(self.name)

    def __init__(self, name, os_type, os_version):
        self.name = name
        self.shortname = name
        self.os_type = os_type
        self.os_version = os_version
        self.commit_name = None
        self.sleeper = None
        self.sleeper_running = Condition()
        self.type = 'container'
        self.commands = Commands()
        self.docker = ['sudo', 'docker', '--dns-search=.', '--dns', socket.gethostbyname(socket.gethostname())]
        self.ssh = Container.SSH(self.name)
        self.user = 'root'

    # verbatim copy/paste from remote.py 10aba9f6c2d2b07af9d8c81aa5e8ff5ee3ce9cd0
    @property
    def os(self):
        if not hasattr(self, '_os'):
            proc = self.run(
                args=[
                    'python', '-c',
                    'import platform; print platform.linux_distribution()'],
                stdout=StringIO(), stderr=StringIO(), check_status=False)
            if proc.exitstatus == 0:
                self._os = OS.from_python(proc.stdout.getvalue().strip())
                return self._os

            proc = self.run(args=['cat', '/etc/os-release'], stdout=StringIO(),
                            stderr=StringIO(), check_status=False)
            if proc.exitstatus == 0:
                self._os = OS.from_os_release(proc.stdout.getvalue().strip())
                return self._os

            proc = self.run(args=['lsb_release', '-a'], stdout=StringIO(),
                            stderr=StringIO())
            self._os = OS.from_lsb_release(proc.stdout.getvalue().strip())
        return self._os

    # verbatim copy/paste from remote.py 10aba9f6c2d2b07af9d8c81aa5e8ff5ee3ce9cd0
    @property
    def arch(self):
        if not hasattr(self, '_arch'):
            proc = self.run(args=['uname', '-m'], stdout=StringIO())
            proc.wait()
            self._arch = proc.stdout.getvalue().strip()
        return self._arch

    def get_tar(self, path, to_path, sudo=False):
        remote_temp_path = tempfile.mktemp()
        args = []
        if sudo:
            args.append('sudo')
        args.extend([
            'tar',
            'cz',
            '-f', remote_temp_path,
            '-C', path,
            '--',
            '.',
            Raw('&&'), 'chmod', '0666', remote_temp_path
            ])
        self.run(args=args)
        self.system('sudo', 'docker', 'cp', self.name + ":/" + remote_temp_path, '/tmp')
        # /tmp on the host is /tmp/tmp in the container
        cmd = "cp /tmp" + remote_temp_path + " " + to_path
        log.info(cmd)
        shutil.copyfile(remote_temp_path, to_path)

    def start(self):
        self.sleeper_running.acquire()
        log.info("sleeper_running  " + str(id(self.sleeper_running)))
        self.sleeper = Thread(target=self.run_sleeper)
        self.sleeper.start()
        self.sleeper_running.wait()
        self.dns_add()

    def dns_add(self):
        cmd = """
ip=$(sudo docker inspect -f "{{ .NetworkSettings.IPAddress }}" {name})
echo host-record={name},$ip | sudo tee /etc/dnsmasq.d/{name}
sudo /etc/init.d/dnsmasq restart
""".replace('{name}', self.name)
        subprocess.check_call(cmd, shell=True)

    def dns_remove(self):
        cmd = """
sudo rm -f /etc/dnsmasq.d/{name}
sudo /etc/init.d/dnsmasq restart
""".replace('{name}', self.name)
        subprocess.check_call(cmd, shell=True)

    def stop(self):
        if self.sleeper:
            self.commands.queue.join()
            self.system('sudo', 'docker', 'stop', self.name);
            self.sleeper.join()
            self.sleeper_running.release()
            self.sleeper = None
        self.dns_remove()

    def commit(self, commit_name):
        self.commit_name = commit_name
        self.system('sudo', 'docker', 'commit', self.name, self.image_name())
        self.stop()

    def check_sleeper(self):
        if self.sleeper and self.sleeper.is_alive():
            return
        if not self.image_exists():
            self.build(self.image_name())
        self.start()

    def image_exists(self):
        image = self.image_name()
        output = subprocess.check_output(["sudo", "docker", "images", image])
        exists = image in output
        log.debug("image '" + image + "' exists in " + output)
        return exists

    def image_name(self):
        image_name = "ceph-base-" + self.os_type + "-" + self.os_version
        if self.commit_name:
            image_name += "-" + self.commit_name
        return image_name.lower()

    def build(self, image):
        origin = self.os_type + ":" + self.os_version
        dockerfile = "FROM " + origin + "\n"
        # workunits.py need git, make
        dockerfile += "RUN apt-get update && apt-get install -y python wget git gcc make automake && mkdir /home/ubuntu \n"
        args = self.docker + ['build', '--tag=' + image, '-']
        log.info("build: running " + " ".join([ "'" + s + "'" for s in args]))
        log.info("build: stdin: " + dockerfile)
        p = subprocess.Popen(args,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(dockerfile)
        if err:
            log.error("completed %s on %s: %s %s" %
                      (str(args), self.name, err, out))
        else:
            log.info("completed %s on %s: %s %s" %
                     (str(args), self.name, err, out))

    def run_sleeper(self):
        args = self.docker + ['run', '--dns-search=.', '--dns', socket.gethostbyname(socket.gethostname()), '--volumes-from', socket.gethostname(), '--privileged', '--rm=true', '--volume', '/tmp:/tmp/tmp', '--name', self.name, '--hostname', self.name, self.image_name(), 'bash', '-c', 'echo running ; sleep 1000000']
        log.info("running " + " ".join([ "'" + s + "'" for s in args]))
        p = subprocess.Popen(args,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             stdin=subprocess.PIPE,
                             close_fds=True)
        self.sleeper_running.acquire()
        for line in iter(p.stdout.readline, ''):
            log.info("run_sleeper: " + line)
            if "running" in line:
                log.info("sleeper_running  " + str(id(self.sleeper_running)))
                self.sleeper_running.notify()
                break
        self.sleeper_running.release()
        log.info("start: container %s started" % self.name);

        out, err = p.communicate(input='')
        if err:
            log.error("completed %s on %s: %s %s" %
                      (str(args), self.name, err, out))
        else:
            log.info("completed %s on %s: %s %s" %
                     (str(args), self.name, err, out))

    def system(self, *args):
        log.info("running " + " ".join([ "'" + str(s) + "'" for s in args]))
        p = subprocess.Popen(args,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,)
        out, err = p.communicate()
        if err:
            log.error("completed %s on %s: %s %s" %
                      (str(args), self.name, err, out))
        else:
            log.info("completed %s on %s: %s %s" %
                     (str(args), self.name, err, out))
        
    def connect(self):
        # see internal.py:connect
        pass

    def run(self, **kwargs):
        self.check_sleeper()
        if type(kwargs['args']) is StringType:
            script = kwargs['args']
        elif type(kwargs['args']) is ListType:
            args = []
            for s in kwargs['args']:
                if type(s) is StringType:
                    if re.search(r'\W', s):
                        args.append("'" + s + "'")
                    else:
                        args.append(s)
                else:
                    args.append(s.value)
            script = " ".join(args)
        else:
            raise type(kwargs['args'])
        with tempfile.NamedTemporaryFile(dir='/tmp/tmp', delete=False) as f:
            tmp = f.name
            f.write(script)
        kwargs['args'] = [ 'bash', tmp ]
        return self.commands.add(self, script, kwargs)

    def write_file(self, path, data):
        if type(data) is types.StringType:
            payload = data
        else:
            payload = data.read()
        with tempfile.NamedTemporaryFile(dir='/tmp/tmp', delete=False) as f:
            tmp = f.name
            f.write(payload)
        return self.run(args=['mv', tmp, path])

    def get_file(self, path, sudo=False, dest_dir='/tmp'):
        assert dest_dir == '/tmp'
        # /tmp/tmp is the shared tmp between all containers
        (fd, tmp) = tempfile.mkstemp(dir='/tmp/tmp')
        os.close(fd)
        self.system('sudo', 'docker', 'exec', self.name, 'cp', path, tmp)
        return tmp

    def sudo_write_file(self, path, data, perms=None, owner=None):
        self.write_file(path, data)
        if perms:
            self.run(args=['chmod', perms, path])
        if owner:
            self.run(args=['chown', owner, path])

    @property
    def system_type(self):
        """
        System type decorator
        """
        return misc.get_system_type(self)
