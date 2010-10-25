#!/usr/bin/env python

'''
scpTsunamiB.py - implements commandqueue in version A to reduce number of threads.

================================================================================
VERSION

How this differs from scpTsunami.py:
This version will initiate transfers before split is finished.
DB.update_chunks_needed() allows this. Also, random insertion of new chunks
Chunks are catted as soon as a host has them all. rm called after cats for all
hosts are done.

================================================================================
The MIT License

Copyright (c) 2010 Clemson University

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

================================================================================
ABOUT

-Brief-
  Python script for distributing large files over a cluster.

-Requirements-
  Unix environment and basic tools (scp, split, rsync)

-Summary-
  This script improves upon the previous version of scpWave by splitting 
  the file to transfer into chunks, similar to bitTorrent. A single host
  starts with the file and splits it into chunks. From there, this initial
  host begins sending out the chunks to target machines. Once a target
  receives a chunk, it may then transfer that chunk to other hosts. This 
  happens until all available hosts have all chunks, at which point the 
  chunks are concatenated into a single file.

-Platform Specific Issues-
  1. scpTsunami relies on a certain format for the split command's 
     output. Has created problems on some machines.
  2. I have noticed that some machines have different versions of rcp.
     If you choose to use rcp, modify RCP_CMD_TEMPLATE with the full
     path the the rcp you want to use.

-Usage Notes-
  Example usage to transfer image.zip to hosts listed in a file:
    ./scpTsunami image.zip images/image.zip -f hosts.txt
    This is the most basic usage. There are several options to control the
    behavior.
    
    To remove chunks for a file:
      ./scpTsunami clean <file> -f <hostfile>
      This will remove any chunks created earlier while transferring <file>.
      <file> will be preserved.

  Behavior
    By default, scp will be used to transfer the files. You can also use rsync
    or rcp. Rsync will only transfer chunks if the file does not already 
    exist on the target host. It does this by comparing the checksums.
    You may use the -p switch to allow chunks to persist on machines 
    and use rsync to keep the chunks updated. This is nice if the transfers
    get interrupted and you need to restart the process.

    See HELP below or run './scpTsunami -h' to see available options.
  
    
================================================================================
DEVELOPMENT NOTES

still need to implement:
  1. what to do if host is up but transfers are still failing? Implement
       maximum consecutive transfer failures.

Ideas for Performance
  - choosing a chunk. choose most rare, can we make them all
    equally available?
  - best chunk size? less than 20M seems much slower. ideal
    appears to be around 25 - 60M
  - playing around with max_transfers_per_host to maximize bandwidth.
  - have hosts favor certain chunks to keep them in memory
  - use rcp instead of scp (no encryption). seems much faster, at
    least for one transfer.
  - start 'cat'ing chunks as soon as they are all received?
    The 'cat'ing should take as long as the slowest one
    since they are done in parallel, may not be worth the complexity.
    Might benefit if host count > max threads
  - avoid retransmission of chunks, say if you interrupt the script. This
    would require disabling of the 'clean up' routine at the end. Could
    use rsync to accomplish this. Also, could check if complete file
    already exists on the target and not run cat. Just set host's
    chunks needed list to empty like the root node.
  - Have option to remove chunks only?
      usage: ./scpTsunami clean <chunkbasename>

To Fix
  1. output from 'split' not consistent on some machines 
     Getting index error with attempting to split the output lines.

Issues
1. this version is slower than the previous! at least for 100MB transfers
   to 7 hosts. find out why.
2. multiple versions of rcp on some machines, having trouble using it.
3. don't have a thread for each subprocess to avoid thread limit?
4. blocking semaphores and ctrl-c behavior. 

updates
  9-22
  transfers start before split has completed
  
'''

import os
import sys
import pty
import time
import Queue
import random
import getopt
import threading
from socket import gethostname
from subprocess import Popen, PIPE, STDOUT

### global data ###
MAX_TRANSFERS_PER_HOST = 6
# most threads will be running a subprocess for scp, so limits the number of
#  concurrent transfers.
MAX_THREADS = 250
MAX_PROCS = 500 # each proc will be a call to rm or cat

# shell commands
SCP_CMD_TEMPLATE = "ssh -o StrictHostKeyChecking=no %s scp -c blowfish \
-o StrictHostKeyChecking=no %s %s:%s"
RCP_CMD_TEMPLATE = "ssh -o StrictHostKeyChecking=no %s rcp %s %s:%s"
CAT_CMD_TEMPLATE = "ssh -o StrictHostKeyChecking=no %s 'cat %s* > %s'"
RM_CMD_TEMPLATE = "ssh -o StrictHostKeyChecking=no %s 'rm -f %s*'"
RSYNC_CMD_TEMPLATE = 'ssh -o StrictHostKeyChecking=no %s rsync -c %s %s:%s'

CHUNK_SIZE = '40m'
LOGGING_ENABLED = False
LOG_FILE = 'scpTsunami.log'
VERBOSE_OUTPUT_ENABLED = False
MAX_FAILCOUNT = 3 # maximum consecutive connection failures allowed per host

def _usage():
    print '''
Usage
 Transfer a file
 ./scpTsunami.py <file> <filedest> [-s][-v] [-u <username>] [-f <hostfile>]
                 [-l '<host1> <host2> ...'] [-r 'basehost[0-1,4-6,...]']
 Remove chunks from a prior transfer
 ./scpTsunami.py clean <file> [-u <username>] [-f <hostfile>]
                 [-l '<host1> <host2> ...'] [-r 'basehost[0-1,4-6,...]'] '''

def _help():
    print '''
Arguments
  If the first argument is 'clean', scpTsunami will attempt to remove chunks
  from a prior transfer of the filename given as the second argument. Else,
  scpTsunami will attempt to transfer the filename given as argument one to
  the path specified in the second argument.

Mandatory options - must use at least 1
  -l '<host1> <host2>' ...     list of hosts to receive the file
  -f <hosts file>              '\\n' separated list of hosts
  -r '<basehost[a-b,c-d,...]>' specify a hostname prefix and a
                               range of numerical suffixes

Other options
  -b    chunk size in bytes, see -b option in unix "split" utility
  -h    help
  -s    log transfer statistics
  -u    specify a username to use
  -v    enable verbose output
  -t    specify maximum number of concurrent transfers per host
  -p    allow chunks to persist on target machines (disables clean up)
 
  --rsync use rsync to transfer or update files based on checksum
  --scp   use scp to transfer files
  --rcp   use rcp to transfer files'''

### End global data ###



### Class Definitions ###


class Spawn:
    ''' 
    inspired by pexpect. source code was used as a reference.
    http://pexpect.sourceforge.net/pexpect.html
    spawn a new process and read the output '''
    def __init__(self, cmd):
        self.cmd = cmd.split()[0]
        self.args = cmd.split()
        self.fptr = None

        self.pid, self.childfd = pty.fork()
        if self.pid == 0:
            os.execvp(self.cmd, self.args)
        self.fptr = os.fdopen(self.childfd, 'r')

    def readline(self):
        try:
            return self.fptr.readline()
        except IOError:
            self.fptr.close()
            return None


class Options:
    ''' Container class for some options to make passing them simple '''
    def __init__(self, logging_enabled=LOGGING_ENABLED, chunksize=CHUNK_SIZE, \
                     verbose_output_enabled=VERBOSE_OUTPUT_ENABLED, \
                     cp_cmd_template=SCP_CMD_TEMPLATE, \
                     rm_cmd_template=RM_CMD_TEMPLATE, \
                     cat_cmd_template=CAT_CMD_TEMPLATE):
        self.logging = logging_enabled
        self.verbose = verbose_output_enabled
        self.username = ''
        self.filename = None      
        self.filedest = None
        self.chunksize = chunksize
        self.chunk_base_name = None
        self.cp_cmd_template = cp_cmd_template
        self.rm_cmd_template = rm_cmd_template
        self.cat_cmd_template = cat_cmd_template
        self.cleanup = True


class Logger:
    ''' Class for providing a log file of the transfers with -s switch '''
    def __init__(self, filename):
        self.filename = filename
        self.starttime = None
        self.completed_transfers = 0
        self.lock = threading.Lock()

    def start(self):
        self.fptr = open(self.filename, 'a')
        self.fptr.write('start ' + time.ctime() + '\n')
        self.starttime = time.time()

    def done(self):
        elapsedt = ' (total = %2.2f)' % (time.time() - self.starttime)
        self.fptr.write('end ' + time.ctime() + elapsedt + '\n\n')
        self.fptr.close()

    def add(self):
        self.lock.acquire()
        self.completed_transfers += 1
        self.fptr.write('%2.2f, %d\n' % (time.time() - self.starttime, \
                                             self.completed_transfers))
        self.lock.release()


class CommandQueue(threading.Thread):
    ''' a threaded queue class for running rm and cat commands. Instead of 
    having a thread for each subprocess, this single thread will create 
    multiple processes. '''
    def __init__(self, procsema):
        threading.Thread.__init__(self)
        self.daemon = True
        self.cmdq = Queue.Queue()
        self.procsema = procsema
        self.flag = threading.Event()
        self.procs = []

    def run(self):
        while True:
            try:
                cmd = self.cmdq.get(timeout=0.5)
                while self.procsema.acquire(blocking=False) == False:
                    # sema is full, free slots
                    if not self.free():
                        time.sleep(0.5)
                # we have a cmd and a semaphore slot, run the cmd
                proc = Popen(cmd, shell=True, stdout=PIPE, stderr=STDOUT)
                self.procs.append(proc)
            except Queue.Empty:
                if self.flag.isSet():
                    break
        # out of loop, wait for running procs
        self.wait_for_procs()

    def free(self):
        ''' try to free a sema slot '''
        activeprocs = []
        slotfreed = False
        for proc in self.procs:
            if proc.poll() is not None: # is proc finished?
                self.procsema.release() # then open slot
                slotfreed = True
            else:
                activeprocs.append(proc)
        self.procs = activeprocs
        # return true if a slot was freed
        return slotfreed

    def wait_for_procs(self):
        ''' wait for procs to finish '''
        while self.procs != []:
            if not self.free():
                time.sleep(0.5)

    def put(self, cmd):
        self.cmdq.put(cmd)

    def finish(self):
        ''' empty the queue then quit '''
        self.flag.set()


class Host:
    ''' Class for representing each host involved in transfers '''
    def __init__(self, hostname, DB, chunks_needed, user, \
                     max_transfers_per_host=MAX_TRANSFERS_PER_HOST, \
                     max_failcount=MAX_FAILCOUNT):
        self.hostname = hostname
        self.transferslots = max_transfers_per_host
        self.chunks_needed = chunks_needed
        self.chunks_owned = []
        self.lock = threading.Lock()
        self.alive = True
        self.DB = DB
        self.user = user
        self.chunk_index = 0 # index into root node's chunks_needed
        self.failcount = 0
        self.max_failcount = max_failcount

    def incFailCount(self):
        ''' called after a transfer to a host fails '''
        self.lock.acquire()
        self.failcount += 1
        self.lock.release()

    def resetFailCount(self):
        self.lock.acquire()
        self.failcount = 0
        self.lock.release()

    def getSlot(self):
        ''' Must call if this host is about to perform a transfer '''
        self.lock.acquire()
        self.transferslots -= 1
        self.lock.release()

    def freeSlot(self):
        ''' Must call after a host completes a transfer '''
        self.lock.acquire()
        self.transferslots += 1
        self.lock.release()

    def isAlive(self):
        ''' Called if a transfer fails to see if the host is up '''
        proc = Popen('ssh -o StrictHostKeyChecking=no %s exit' % \
                         (self.user+self.hostname), shell=True)
        ret = proc.wait() # 0 means alive
        return not ret

    def setDead(self):
        ''' If isAlive() failed and host is down, call this to 
        stop attempts at using this host '''
        self.lock.acquire()
        if self.alive:
            self.alive = False
            self.DB.incDeadHosts()
            self.transferslots = 0
        self.lock.release()


class Chunk:
    ''' Class for each chunk '''
    def __init__(self, filename):
        self.filename = filename


class Database:
    ''' Database for keeping track of all participating hosts '''
    def __init__(self):
        self.hostlist = []
        self.hosts_with_file = 1
        self.lock = threading.Lock()
        self.hostcount = 0
        self.tindex = 0
        self.chunkCount = 0
        self.split_complete = False
        self.deadhosts = 0
        self.roothost = None

    def incDeadHosts(self):
        ''' call after setting a Host instance as dead, lets the script
        know when to stop attempts at matching seeds and targets '''
        self.lock.acquire()
        self.deadhosts += 1
        self.lock.release()

    def hostDone(self):
        ''' call after host has all chunks so we know when to stop '''
        self.lock.acquire()
        self.hosts_with_file += 1
        self.lock.release()

    # this version will return an available transfer if one exists
    def getTransfer(self):
        ''' Returns (seed, target, chunk) which will be passed to a transfer 
        thread '''
        for q in xrange(self.hostcount):
            # choose a target
            self.tindex = (self.tindex+1) % self.hostcount
            # check if chosen target is alive and has an open slot
            if self.hostlist[self.tindex].transferslots > 0 and \
                    self.hostlist[self.tindex].alive is True: 
                # transfer first chunk needed we find
                for chunk in self.hostlist[self.tindex].chunks_needed:
                    # now, find a seed with the needed chunk
                    
                    # random seed choice
                    sindex = random.randint(0, self.hostcount)
                    # or fixed first seed
                    #sindex = self.tindex
                    for i in xrange(self.hostcount):
                        # +/- 1 may affect some things
                        sindex = (sindex - 1) % self.hostcount
                        if chunk in self.hostlist[sindex].chunks_owned and \
                                self.hostlist[sindex].transferslots > 0 and \
                                self.hostlist[sindex].alive is True:
                            # found a seed
                            self.hostlist[sindex].getSlot()
                            self.hostlist[self.tindex].getSlot() # right spot?
                            return sindex, self.tindex, chunk
        # couldn't match up a transfer
        return None, None, None

    # update chunks_needed list of each host
    def update_chunks_needed(self):
        ''' this method is called from split_file() everytime a new chunk has
        been created. It updates the chunks needed lists of each host '''
        for host in self.hostlist:
            if host == self.roothost: continue # root is updated in split_file()
            if host.chunk_index < (self.chunkCount):
                new_chunks = self.roothost.chunks_owned[host.chunk_index:]
                #host.chunks_needed += new_chunks # method 1
                for chunk in new_chunks: # method 2
                    host.chunks_needed.insert( \
                        random.randint(0, len(host.chunks_needed)+1), chunk)
                # want to randomize order of chunks in list
                #random.shuffle(host.chunks_needed) # method 3
                host.chunk_index += len(new_chunks)
        

class Transfer(threading.Thread):
    ''' This class handles the subprocess creation for calls to rcp/scp'''
    def __init__(self, DB, seedindex, chunk, targetindex, threadlist, \
                     threadsema, options, logger, commandq):
        threading.Thread.__init__(self)
        self.DB = DB
        self.seedindex = seedindex
        self.chunk = chunk
        self.targetindex = targetindex
        self.threadlist = threadlist
        self.threadsema = threadsema
        self.options = options
        self.logger = logger
        self.commandq = commandq

        # might need to tweak this a little more
        self.cp_cmd = options.cp_cmd_template % \
            (options.username+DB.hostlist[seedindex].hostname, chunk.filename, \
                 options.username + DB.hostlist[targetindex].hostname, \
                 chunk.filename)

    def run(self):
        target = self.DB.hostlist[self.targetindex]
        seed = self.DB.hostlist[self.seedindex]

        try:
            proc = Popen(self.cp_cmd, shell=True, stdout=PIPE, stderr=PIPE)
            stdout, stderr = proc.communicate()
            ret = proc.wait()

            if ret == 0:
                # transfer succeeded
                if False:#self.options.verbose:
                    print '%s(%d) -> %s(%d) : (%s) success' % \
                        (seed.hostname, seed.transferslots, target.hostname, \
                             target.transferslots, self.chunk.filename)
                target.chunks_owned.append(self.chunk)
                # check if target has all the chunks
                if self.DB.split_complete and len(target.chunks_owned) == \
                        self.DB.chunkCount:
                    # target host has all the chunks
                    self.DB.hostDone()
                    if self.options.logging:
                        self.logger.add()
                    # cat the chunks
                    catCmd = self.options.cat_cmd_template % \
                        (self.options.username+target.hostname, \
                             self.options.chunk_base_name, self.options.filedest)
                    self.commandq.put(catCmd)

            else:
                # transfer failed?
                if self.chunk not in target.chunks_needed:
                    target.chunks_needed.append(self.chunk)
                if self.options.verbose:
                    print '%s(%d) -> %s(%d) : (%s) failed' % \
                        (seed.hostname, seed.transferslots, \
                             target.hostname, target.transferslots, \
                             self.chunk.filename)
                    print stderr,
                # check if host is up and accepting ssh connections
                if not target.isAlive():
                    target.setDead()
                if not seed.isAlive():
                    seed.setDead()

        except Exception:
            # failed to start transfer?
            if self.chunk not in target.chunks_needed:
                target.chunks_needed.append(self.chunk)
            print 'ERROR Transfer.run: ', sys.exc_info()[1]

        # free up transfer slots on the seed and target
        target.freeSlot()
        seed.freeSlot()
        self.threadlist.remove(self)
        self.threadsema.release()

### END class definitions ###

def initiateTransfers(DB, threadsema, options, logger, threadlist, commandq):
    ''' Returns once all chunks have been transferred to available hosts '''

    # loop until every available host has the entire file
    while DB.hosts_with_file + DB.deadhosts < DB.hostcount:
        seedindex, targetindex, chunk = DB.getTransfer()
        if chunk:
            # begin the transfer
            threadsema.acquire()
            DB.hostlist[targetindex].chunks_needed.remove(chunk)
            transferThread = Transfer( \
                DB, seedindex, chunk, targetindex, threadlist, threadsema, \
                    options, logger, commandq)
            threadlist.append(transferThread)
            transferThread.start()
        else:
            # having sleep prevents repeated failed calls to DB.getTransfer()
            # but the sleep() may also slow it down..
            time.sleep(0.2)

    # wait for transfers to complete before returning
    while threadlist != []:
        time.sleep(0.5)


def split_file(DB, options):
    ''' split a file into chunks as a separate thread so transfers may begin
    as soon as first chunk is created. 
    NOTE - this function relies on a certain output format for split. '''
    s = Spawn('split --verbose -b %s %s %s' % \
                  (options.chunksize, options.filename, options.chunk_base_name))

    curname = s.readline().split()[2].strip("`'")
    while curname:
        try:
            prevname = s.readline().split()[2].strip("`'")
        except Exception:
            #print 'err split', sys.exc_info()[1]
            prevname = None
        DB.roothost.chunks_owned.append(Chunk(curname))
        DB.chunkCount += 1
        DB.update_chunks_needed()
        curname = prevname

    print 'split complete!'
    DB.split_complete = True


def main():
    # init defaults
    options = Options()
    max_threads = MAX_THREADS
    max_procs = MAX_PROCS
    max_transfers_per_host = MAX_TRANSFERS_PER_HOST
    rm_cmd_template = RM_CMD_TEMPLATE
    cat_cmd_template = CAT_CMD_TEMPLATE
    logfile = LOG_FILE
    cleanonly = False # just remove chunks and exit

    # get the command line options
    try:
        optlist, args = getopt.gnu_getopt( \
            sys.argv[1:], 't:u:f:r:l:b:svhp', ['help', 'rsync', 'scp', 'rcp'])
        for opt, arg in optlist:
            if opt == '-h' or opt == '--help':
                _usage()
                _help()
                sys.exit(0)
    except Exception:
        print 'ERROR options: ', sys.exc_info()[1]
        sys.exit(1)

    if len(args) < 2:
        print 'ERROR: 2 args required'
        _usage()
        sys.exit(1)

    # get name of file to transfer
    try:
        if args[0] == 'clean':
            cleanonly = True
            options.filename = args[1]
        else:
            options.filename = args[0]
            filepath = os.path.abspath(options.filename)
            if not os.path.isfile(filepath):
                print 'ERROR: %s not found' % filepath
                sys.exit(1)
            options.filedest = args[1]
    except Exception:
        print 'ERROR: %s' % sys.exc_info()[1]
        _usage()
        sys.exit(1)


    # parse the command line
    targetlist = [] # takes Host(hostname) 
    for opt, arg in optlist:
        if opt == '-f': # file
            # read '\n' separated hosts from file
            try:
                hostfile = open(arg, 'r')
            except Exception:
                print 'ERROR: Failed to open hosts file:', arg
                sys.exit(1)
            for host in hostfile.readlines():
                targetlist.append(host.split('\n')[0].strip())
            hostfile.close()
        elif opt == '-r':
            try:
                # format: -r <basehost[0-1,3-3,5-11...]>
                # eg. -r host[1-2,4-5] generates host1, host2, host4, host5
                arg = arg.replace(' ','')
                basehost = arg.split('[')[0]
                # get 3 part ranges eg: ['1-3','5-5']
                ranges = arg.split('[')[1].strip('[]').split(',')
                for rng in ranges:
                    first = rng.split('-')[0]
                    last = rng.split('-')[1]
                    for num in range(int(first), int(last)+1):
                        leadingZeros = len(first) - len(str(num))
                        host = basehost + '0'*leadingZeros + str(num)
                        targetlist.append(host)
            except Exception:
                print 'ERROR: Invalid argument for -r:', arg
                print sys.exc_info()[1]
                _usage()
                sys.exit(1)
        elif opt == '-l':
            # read quoted list of comma separated hosts from command line
            hostlist = arg.split()
            for host in hostlist:
                targetlist.append(host.strip())
        elif opt == '-b':
            options.chunksize = arg
        elif opt == '-u': # username
            options.username = arg + '@'
        elif opt == '-s': # log transfer statistics
            options.logging = True
        elif opt == '-v': # verbose output
            options.verbose = True
        elif opt == '-t': # transfers per host
            max_transfers_per_host = int(arg)
        elif opt == '-p': # chunk persistence
            options.cleanup = False
        elif opt == '--rsync': # use rsync
            options.cp_cmd_template = RSYNC_CMD_TEMPLATE
        elif opt == '--scp':   # use scp
            options.cp_cmd_template = SCP_CMD_TEMPLATE
        elif opt == '--rcp':   # use rcp
            options.cp_cmd_template = RCP_CMD_TEMPLATE


    # set up a list database of all hosts
    DB = Database()
    DB.hostlist.append(Host(gethostname(), DB, [], options.username, \
                                max_transfers_per_host))
    DB.roothost = DB.hostlist[0]
    targetlist = set(targetlist) # remove duplicates
    for target in targetlist:
        DB.hostlist.append(Host(target, DB, [], options.username, \
                                    max_transfers_per_host))
    DB.hostcount = len(DB.hostlist)

    # build prefix for file chunk names
    options.chunk_base_name = \
        os.path.join('/tmp', os.path.split(options.filename)[-1])+ '.chunk_'

    # initialize the background command queue thread
    procsema = threading.Semaphore(max_procs)
    commandq = CommandQueue(procsema)
    commandq.start()

    if cleanonly is True:
        # remove chunks from hosts and exit
        print 'removing chunks ...'
        for host in DB.hostlist:
            rmCmd = rm_cmd_template % \
                (options.username+host.hostname, options.chunk_base_name)
            commandq.put(rmCmd)
        commandq.finish()
        while commandq.isAlive():
            time.sleep(0.5)
        print 'done'
        sys.exit(0)

    # split the file to transfer. chunk_base_name is the prefix for chunk names
    split_thread = threading.Thread(target=split_file, args=(DB, options))
    split_thread.start()

    # semaphore to limit thread creation
    threadsema = threading.Semaphore(max_threads)
    threadlist = []

    ##### Initiate the transfers #####
    logger = None
    if options.logging:
        logger = Logger(logfile)
        logger.start()
    print 'transferring %s to %d hosts ...' %(options.filename, len(targetlist))
    try:
        # returns once transfers are complete
        initiateTransfers(DB, threadsema, options, logger, threadlist, commandq)
        print '%d transfers complete' % (DB.hosts_with_file - 1)
    except Exception:
        print 'ERROR ', sys.exc_info()[1]
    except KeyboardInterrupt:
        print '[!] caught interrupt'

    # in case transfers were interrupted, let threads finish execution.
    while threadlist != []: 
        time.sleep(0.5)

    # must wait for cat processes to finish
    if options.cleanup is True:
        # rm all chunks
        commandq.wait_for_procs()
        for host in DB.hostlist:
            rmCmd = rm_cmd_template % \
                (options.username+host.hostname, options.chunk_base_name)
            commandq.put(rmCmd)

    # wait for procs to finish
    commandq.finish()
    while commandq.isAlive():
        time.sleep(0.5)

    # terminate the log file
    if options.logging:
        logger.done()


if __name__ == '__main__':
    try:
        main()
        print 'done'
    except Exception:
        print 'ERROR! ', sys.exc_info()[1]
        print 'exiting ...'
    except KeyboardInterrupt:
        print 'exiting ...'
