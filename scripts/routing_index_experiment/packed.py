"""Compile the existing index into one immutable, process-local SoA byte buffer.

Header (8 uint32): node/edge/ID/label counts and offsets to the four regions.
Nodes: seven uint32 columns (edge start/count, wildcard, fallback start/count,
terminal start/count). Edges: three columns (label byte offset/length, child ID).
IDs: flat uint32 candidate ordinals. Labels: UTF-8 bytes, including empty segments.
Integer regions use native byte order; this is not a portable persistence format.
"""

import ctypes
from array import array
from contextlib import contextmanager
from unittest.mock import patch

from fastapi import routing

MISSING = 0xFFFFFFFF


class PackedIndex:
    def __init__(self, candidates):
        # The baseline builder remains authoritative for eligibility/fallbacks.
        # Its object graph is temporary and is discarded after compilation.
        tree = routing._FrozenRouteIndex(candidates)
        self.candidates = tree.candidates
        nodes = [tree.root]
        node_ids = {id(tree.root): 0}
        for node in nodes:
            for child in node.children.values():
                node_ids[id(child)] = len(nodes)
                nodes.append(child)
        columns = [[] for _ in range(7)]
        edges = [[] for _ in range(3)]
        ordinals = []
        labels = bytearray()
        interned = {}
        for node in nodes:
            columns[0].append(len(edges[0]))
            for label, child in node.children.items():
                if label is None:
                    continue
                encoded = label.encode("utf-8")
                if encoded not in interned:
                    interned[encoded] = len(labels)
                    labels.extend(encoded)
                edges[0].append(interned[encoded])
                edges[1].append(len(encoded))
                edges[2].append(node_ids[id(child)])
            columns[1].append(len(edges[0]) - columns[0][-1])
            wildcard = node.children.get(None)
            columns[2].append(MISSING if wildcard is None else node_ids[id(wildcard)])
            for start_column, values in ((3, node.fallback), (5, node.terminal)):
                columns[start_column].append(len(ordinals))
                columns[start_column + 1].append(len(values))
                ordinals.extend(values)
        n, e, k = len(nodes), len(edges[0]), len(ordinals)
        word_count = 8 + 7 * n + 3 * e + k
        words = array(
            "I", [n, e, k, len(labels), 8, 8 + 7 * n, 8 + 7 * n + 3 * e, word_count * 4]
        )
        if words.itemsize != 4:
            raise RuntimeError("Packed experiment requires 32-bit unsigned int")
        for column in columns + edges:
            words.extend(column)
        words.extend(ordinals)
        self.buffer = words.tobytes() + labels
        self.words = memoryview(self.buffer)[: word_count * 4].cast("I")
        self.labels = memoryview(self.buffer)[word_count * 4 :]

    def select(self, path):
        words = self.words
        n, e = words[0], words[1]
        ns, es, ids = words[4], words[5], words[6]
        nodes = [0]
        ordinals = []
        for segment in path.encode("utf-8").split(b"/"):
            next_nodes = []
            for node in nodes:
                start = words[ns + 3 * n + node]
                count = words[ns + 4 * n + node]
                ordinals.extend(words[ids + start : ids + start + count])
                wildcard = words[ns + 2 * n + node]
                if wildcard != MISSING:
                    next_nodes.append(wildcard)
                start = words[ns + node]
                for edge in range(start, start + words[ns + n + node]):
                    offset = words[es + edge]
                    length = words[es + e + edge]
                    if (
                        length == len(segment)
                        and self.labels[offset : offset + length] == segment
                    ):
                        next_nodes.append(words[es + 2 * e + edge])
            nodes = next_nodes
        for node in nodes:
            for column in (3, 5):
                start = words[ns + column * n + node]
                count = words[ns + (column + 1) * n + node]
                ordinals.extend(words[ids + start : ids + start + count])
        for ordinal in sorted(ordinals):
            yield self.candidates[ordinal]


class NativeIndex(PackedIndex):
    def __init__(self, candidates, library):
        super().__init__(candidates)
        self.library = library
        self.pointer = ctypes.c_char_p(self.buffer)

    def select(self, path):
        encoded = path.encode("utf-8")
        output = (ctypes.c_uint32 * len(self.candidates))()
        count = self.library.packed_select(
            self.pointer, encoded, len(encoded), output, len(output)
        )
        if count < 0:
            raise RuntimeError("Native traversal allocation/capacity failure")
        for index in range(count):
            yield self.candidates[output[index]]


def load_native(path):
    library = ctypes.CDLL(str(path))
    library.packed_select.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_size_t,
    ]
    library.packed_select.restype = ctypes.c_int64
    return library


@contextmanager
def layout(name, library=None):
    original = routing._FrozenRouteIndex
    if name == "object":
        yield
        return

    # PackedIndex uses the unmodified builder; replace only its construction at
    # freeze time, not the builder's implementation or the runtime matchers.
    def factory(candidates):
        with patch.object(routing, "_FrozenRouteIndex", original):
            if name == "native":
                return NativeIndex(candidates, library)
            if name == "packed":
                return PackedIndex(candidates)
            raise ValueError(name)

    with patch.object(routing, "_FrozenRouteIndex", factory):
        yield
