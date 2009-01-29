#===============================================================================
# Copyright 2008 Matt Chaput
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#    http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#===============================================================================

"""
Generic storage classes for creating static files that support
FAST key-value (Table*) and key-value-postings (PostingTable*) storage.

These objects require that you add rows in increasing order of their
keys. They will raise an exception you try to add keys out-of-order.

These objects use a simple file format. The first 4 bytes are an unsigned
long ("!L" struct) pointing to the directory data.
The next 4 bytes are a pointer to the posting data, if any. In a table without
postings, this is 0.
Following that are N pickled objects (the blocks of rows).
Following the objects is the directory, which is a pickled list of
(key, filepos) pairs. Because the keys are pickled as part of the directory,
they can be any pickle-able object. (The keys must also be hashable because
they are used as dictionary keys. It's best to use value types for the
keys: tuples, numbers, and/or strings.)

This module also contains simple implementations for writing and reading
static "Record" files made up of fixed-length records based on the
struct module.
"""

import cPickle, shutil, tempfile
from bisect import bisect_left, bisect_right

try:
    from zlib import compress, decompress
    has_zlib = True
except ImportError:
    has_zlib = False

from whoosh.structfile import StructFile

# Exceptions

class ItemNotFound(Exception):
    pass

# Utility functions

def copy_data(treader, inkey, twriter, outkey, postings = False, buffersize = 32 * 1024):
    """
    Copies the data associated with the key from the
    "reader" table to the "writer" table, along with the
    raw postings if postings = True.
    """
    
    if postings:
        (offset, length), postcount, data = treader._get(inkey)
        super(twriter.__class__, twriter).add_row(outkey, ((twriter.offset, length), postcount, data))
        
        # Copy the raw posting data
        infile = treader.table_file
        infile.seek(treader.postpos + offset)
        outfile = twriter.posting_file
        if length <= buffersize:
            outfile.write(infile.read(length))
        else:
            sofar = 0
            while sofar < length:
                readsize = min(buffersize, length - sofar)
                outfile.write(infile.read(readsize))
                sofar += readsize
        
        twriter.offset = outfile.tell()
    else:
        twriter.add_row(outkey, treader[inkey])


# Table writer classes

class TableWriter(object):
    def __init__(self, table_file, blocksize = 64 * 1024,
                 compressed = 0, postings = False, stringids = False):
        self.table_file = table_file
        self.blocksize = blocksize
        
        if compressed > 0 and not has_zlib:
            raise Exception("zlib is not available: cannot compress table")
        self.compressed = compressed
        
        self.haspostings = postings
        if postings:
            self.offset = 0
            self.postcount = 0
            self.lastpostid = None
            self.stringids = stringids
            self.posting_file = StructFile(tempfile.TemporaryFile())
        
        self.rowbuffer = []
        self.lastkey = None
        self.blockfilled = 0
        
        self.dir = []
        
        # Remember where we started writing
        self.start = table_file.tell()
        # Save space for a pointer to the directory
        table_file.write_ulong(0)
        # Save space for a pointer to the postings
        table_file.write_ulong(0)
        
        self.options = {"haspostings": postings,
                        "compressed": compressed,
                        "stringids": stringids}
    
    def close(self):
        # If there is still a block waiting to be written, flush it out
        if self.rowbuffer:
            self._write_block()
        
        tf = self.table_file
        haspostings = self.haspostings
        
        # Remember where we started writing the directory
        dirpos = tf.tell()
        # Write the directory
        tf.write_pickle((tuple(self.dir), self.options))
        
        if haspostings:
            # Remember where we started the postings
            postpos = tf.tell()
            # Seek back to the beginning of the postings and
            # copy them onto the end of the table file.
            self.posting_file.seek(0)
            shutil.copyfileobj(self.posting_file, tf)
            self.posting_file.close()
        
        # Seek back to where we started writing and write a
        # pointer to the directory
        tf.seek(self.start)
        tf.write_ulong(dirpos)
        
        if haspostings:
            # Write a pointer to the postings
            tf.write_ulong(postpos)
        
        tf.close()
    
    def _write_block(self):
        buf = self.rowbuffer
        key = buf[0][0]
        compressed = self.compressed
        
        self.dir.append((key, self.table_file.tell()))
        if compressed:
            pck = cPickle.dumps(buf)
            self.table_file.write_string(compress(pck, compressed))
        else:
            self.table_file.write_pickle(buf)
        
        self.rowbuffer = []
        self.blockfilled = 0
    
    def write_posting(self, id, data, writefn):
        # IDs must be added in increasing order
        if id <= self.lastpostid:
            raise IndexError("IDs must increase: %r..%r" % (self.lastpostid, id))
        
        pf = self.posting_file
        if self.stringids:
            pf.write_string(id.encode("utf8"))
        else:
            lastpostid = self.lastpostid or 0
            pf.write_varint(id - lastpostid)
        
        self.lastpostid = id
        self.postcount += 1
        
        return writefn(pf, data)
    
    def add_row(self, key, data):
        # Note: call this AFTER you add any postings!
        # Keys must be added in increasing order
        if key <= self.lastkey:
            raise IndexError("Keys must increase: %r..%r" % (self.lastkey, key))
        
        rb = self.rowbuffer
        
        # Ugh! We're pickling twice! At least it's fast.
        self.blockfilled += len(cPickle.dumps(data, -1))
        self.lastkey = key
        
        if self.haspostings:
            # Add the posting info to the stored row data
            endoffset = self.posting_file.tell()
            length = endoffset - self.offset
            rb.append((key, (self.offset, length, self.postcount, data)))
            
            # Reset the posting variables
            self.offset = endoffset
            self.postcount = 0
            self.lastpostid = None
        else:
            rb.append((key, data))
        
        # If this row filled up a block, flush it out
        if self.blockfilled >= self.blocksize:
            self._write_block()


# Table reader classes

class TableReader(object):
    def __init__(self, table_file):
        self.table_file = table_file
        
        # Read the pointer to the directory
        dirpos = table_file.read_ulong()
        # Read the pointer to the postings (0 if there are no postings)
        self.postpos = table_file.read_ulong()
        
        # Seek to where the directory begins and read it
        table_file.seek(dirpos)
        dir, options = table_file.read_pickle()
        self.__dict__.update(options)
        if self.compressed > 0 and not has_zlib:
            raise Exception("zlib is not available: cannot decompress table")
        
        # Break the directory out
        self.blockindex, self.blockpositions = zip(*dir)
        self.blockcount = len(dir)
        
        # Initialize cached block
        self.currentblock = None
        self.itemlist = None
        self.itemdict = None
        
        if self.haspostings:
            self._read_id = self._read_id_string if self.stringids else self._read_id_varint
            self.get = self._get_ignore_postinfo
        else:
            self.get = self._get_plain
    
    def __contains__(self, key):
        self._load_block(key)
        return key in self.itemdict
    
    def _get_ignore_postinfo(self, key):
        self._load_block(key)
        return self.itemdict[key][3]
    
    def _get_plain(self, key):
        self._load_block(key)
        return self.itemdict[key]
    
    def __iter__(self):
        if self.haspostings:
            for i in xrange(0, self.blockcount):
                self._load_block_num(i)
                for key, value in self.itemlist:
                    yield (key, value[3])
        else:
            for i in xrange(0, self.blockcount):
                self._load_block_num(i)
                for key, value in self.itemlist:
                    yield (key, value)
    
    def _read_id_varint(self, lastid):
        return lastid + self.table_file.read_varint()
    
    def _read_id_string(self, lastid):
        return self.table_file.read_string().decode("utf8")
    
    def iter_from(self, key):
        postings = self.haspostings
        
        self._load_block(key)
        blockcount = self.blockcount
        itemlist = self.itemlist
        itemlen = len(itemlist)
        
        p = bisect_left(itemlist, (key, None))
        
        # Yield the rest of the rows
        while True:
            kv = itemlist[p]
            if postings:
                yield (kv[0], kv[1][3])
            else:
                yield kv
            
            p += 1
            if p >= itemlen:
                if self.currentblock >= blockcount - 1:
                    return
                self._load_block_num(self.currentblock + 1)
                itemlist = self.itemlist
                itemlen = len(itemlist)
                p = 0
    
    def close(self):
        self.table_file.close()
    
    def keys(self):
        return (key for key, _ in self)
    
    def values(self):
        return (value for _, value in self)
    
    def posting_count(self, key):
        if not self.haspostings: raise Exception("This table does not have postings")
        return self._get_plain(key)[2]
    
    def postings(self, key, readfn):
        postfile = self.table_file
        _read_id = self._read_id
        id = 0
        for _ in xrange(0, self._seek_postings(key)):
            id = _read_id(id)
            yield (id, readfn(postfile))
    
    def _load_block_num(self, bn):
        if bn < 0 or bn >= len(self.blockindex):
            raise ValueError("Block number %s/%s" % (bn, len(self.blockindex)))
        
        pos = self.blockpositions[bn]
        self.table_file.seek(pos)
        
        if self.compressed:
            pck = self.table_file.read_string()
            itemlist = cPickle.loads(decompress(pck))
        else:
            itemlist = self.table_file.read_pickle()
        
        self.itemlist = itemlist
        self.itemdict = dict(itemlist)
        self.currentblock = bn
        self.minkey = itemlist[0][0]
        self.maxkey = itemlist[-1][0]
    
    def _load_block(self, key):
        if self.currentblock is None or key < self.minkey or key > self.maxkey:
            bn = max(0, bisect_right(self.blockindex, key) - 1)
            self._load_block_num(bn)

    def _seek_postings(self, key):
        offset, length, count = self._get_plain(key)[:3] #@UnusedVariable
        self.table_file.seek(self.postpos + offset)
        return count



if __name__ == '__main__':
    pass
    
    
    
    
    
    
    
    
    
    
    
    
    
    
