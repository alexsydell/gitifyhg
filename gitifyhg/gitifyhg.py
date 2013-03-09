# Copyright 2012-2013 Dusty Phillips

# This file is part of gitifyhg.

# gitifyhg is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# gitifyhg is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with gitifyhg.  If not, see <http://www.gnu.org/licenses/>.

# Some of this code comes from https://github.com/felipec/git/tree/fc/remote/hg
# but much of it has been rewritten.


import sys
import os
import re
import subprocess
from path import path as p

# Enable "plain" mode to make us resilient against changes to the locale, as we
# rely on parsing certain messages produced by Mercurial. See issue #26.
os.environ['HGPLAIN'] = '1'
# Disable loading of the user's $HOME/.hgrc as extensions can cause weird
# interactions and it's better to run in a known state.
os.environ['HGRCPATH'] = ''


from mercurial.ui import ui
from mercurial.error import Abort, RepoError
from mercurial.util import sha1
from mercurial import hg
from mercurial.bookmarks import listbookmarks, readcurrent

from .util import log, die, output, actual_stdout, HGMarks, hg_to_git_spaces
from .hgimporter import HGImporter
from .gitexporter import GitExporter


class GitRemoteParser(object):
    '''Parser for stdin that processes the git-remote protocol.'''

    def __init__(self):
        self.peek_stack = []
        self.read_line()

    def read_line(self):
        '''Read a line from the standard input.'''
        if self.peek_stack:
            self.line = self.peek_stack.pop(0)
        else:
            self.line = sys.stdin.readline().strip()
        log("INPUT: %s" % self.line)
        return self.line

    def peek(self):
        '''Look at the next line and store it so that it can still be returned
        by read_line.'''
        line = sys.stdin.readline().strip()
        self.peek_stack.append(line)
        return line

    def read_mark(self):
        '''The remote protocol contains lines of the format mark: number.
        Return the mark.'''
        return int(self.read_line().partition(':')[-1])

    def read_data(self):
        '''Read all data following a data line for the given number of bytes'''
        self.read_line()
        if not self.line.startswith('data'):
            return None
        size = int(self.line.partition(' ')[-1])
        return sys.stdin.read(size)

    def read_author(self):
        '''Read and parse an author string. Return a tuple of
        (user string, date, git_tz).'''
        self.read_line()
        AUTHOR_RE = re.compile(r'^(?:author|committer|tagger)(?: ([^<>]+)?)? <([^<>]*)> (\d+) ([+-]\d+)')
        match = AUTHOR_RE.match(self.line)
        if not match:
            return None

        user, email, date, tz = match.groups()
        if user is None:
            user = ''
        user += ' <' + email + '>'

        date = int(date)
        tz = -((int(tz) / 100) * 3600) + ((int(tz) % 100) * 60)
        return (user, date, tz)

    def read_block(self, sentinel):
        '''Yield a block of lines one by one until the sentinel value
        is returned. Sentinel may be an empty string, 'done', or other values
        depending on what block is being read.'''
        while self.line != sentinel:
            yield self.line
            self.line = self.read_line()


class HGRemote(object):
    def __init__(self, alias, url):
        if hg.islocal(url.encode('utf-8')):
            url = p(url).abspath()
            # Force git to use an absolute path in the future
            cmd = ['git', 'config', 'remote.%s.url' % alias, "gitifyhg::%s" % url]
            subprocess.call(cmd)

        # use hash of URL as unique identifier in various places.
        # this has the advantage over 'alias' that it stays constant
        # when the user does a "git remote rename old new".
        self.uuid = sha1(url.encode('utf-8')).hexdigest()

        gitdir = p(os.environ['GIT_DIR'].decode('utf-8'))
        self.remotedir = gitdir.joinpath('hg', self.uuid)
        self.marks_git_path = self.remotedir.joinpath('marks-git')
        self.marks = HGMarks(self.remotedir.joinpath('marks-hg'))
        self.parsed_refs = {}
        self.blob_marks = {}
        self.branches = {}
        self.bookmarks = {}

        self.prefix = 'refs/hg/%s' % alias
        self.alias = alias
        self.url = url
        self.build_repo(url)

    def build_repo(self, url):
        '''Make the Mercurial repo object self.repo available. If the local
        clone does not exist, clone it, otherwise, ensure it is fetched.'''
        myui = ui()
        myui.setconfig('ui', 'interactive', 'off')
        myui.setconfig('extensions', 'mq', '')

        local_path = self.remotedir.joinpath('clone')
        if not local_path.exists():
            try:
                self.peer, dstpeer = hg.clone(myui, {}, url.encode('utf-8'),
                    local_path.encode('utf-8'), update=False, pull=True)
            except (RepoError, Abort) as e:
                sys.stderr.write("abort: %s\n" % e)
                if e.hint:
                    sys.stderr.write("(%s)\n" % e.hint)
                sys.exit(-1)

            self.repo = dstpeer.local()
        else:
            self.repo = hg.repository(myui, local_path.encode('utf-8'))
            self.peer = hg.peer(myui, {}, url.encode('utf-8'))
            self.repo.pull(self.peer, heads=None, force=True)

        self.marks.upgrade_marks(self.repo)

    def process(self):
        '''Process the messages coming in on stdin using the git-remote
        protocol and respond appropriately'''
        parser = GitRemoteParser()

        for line in parser.read_block(''):
            command = line.split()[0]
            if command not in ('capabilities', 'list', 'import', 'export'):
                die('unhandled command: %s' % line)
            getattr(self, 'do_%s' % command)(parser)
            actual_stdout.flush()

        self.marks.store()

    def do_capabilities(self, parser):
        '''Process the capabilities request when incoming from git-remote.
        '''
        output(u"import")
        output(u"export")
        output(u"refspec refs/heads/branches/*:%s/branches/*" % self.prefix)
        output(u"refspec refs/heads/*:%s/bookmarks/*" % self.prefix)
        output(u"refspec refs/tags/*:%s/tags/*" % self.prefix)

        if self.marks_git_path.exists():
            output(u"*import-marks %s" % self.marks_git_path)
        output(u"*export-marks %s" % self.marks_git_path)

        output()

    def do_list(self, parser):
        '''List all references in the mercurial repository. This includes
        the current head, all branches, tags, and bookmarks.'''

        current_branch = self.repo.dirstate.branch()

        # Update the head reference
        head = readcurrent(self.repo)
        if head:
            node = self.repo[head]
        else:
            # If there is no bookmark for head, mock one
            head = current_branch
            node = self.repo['.']
            # I think this means an initial clone occured and we haven't
            # hg updated yet in the local clone
            if not node:
                if 'default' in self.repo:
                    node = self.repo['default']
                else:  # empty repository or head is at 0 commit
                    output()
                    return
            head = head if head != 'default' else 'master'
            #self.bookmarks[head] = node

        self.headnode = (head, node)

        # Update the bookmark references
        for bookmark, node in listbookmarks(self.repo).iteritems():
            self.bookmarks[bookmark] = self.repo[node]

        # update the named branch references
        for branch in self.repo.branchmap():
            heads = self.repo.branchheads(branch, closed=True)
            if heads:
                self.branches[branch] = heads

        # list the head reference
        output("@refs/heads/%s HEAD" % self.headnode[0])

        # list the named branch references
        for branch in self.branches:
            if branch != "default":
                output("? refs/heads/branches/%s" % hg_to_git_spaces(branch))
            else:
                output("? refs/heads/master")

        # list the bookmark references
        for bookmark in self.bookmarks:
            if bookmark != "master":
                output("? refs/heads/%s" % hg_to_git_spaces(bookmark))

        # list the tags
        for tag, node in self.repo.tagslist():
            if tag != "tip":
                output("? refs/tags/%s" % hg_to_git_spaces(tag))

        output()

    def do_import(self, parser):
        HGImporter(self, parser).process()

    def do_export(self, parser):
        GitExporter(self, parser).process()


def main():
    '''Main entry point for the git-remote-gitifyhg command. Parses sys.argv
    and constructs a parser from the result.
    '''
    log("WTF")
    HGRemote(*[x.decode('utf-8') for x in sys.argv[1:3]]).process()
    try:
        sys.stderr.close()
    except:
        pass


if __name__ == '__main__':
    sys.exit(main())
