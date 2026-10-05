"""Disjoint-set union over string keys.

Used twice in this pipeline: step 1 merges the duplicate sightings of one
entity across overlapping tiles, and step 3 clusters related processes into
HAZOP nodes. Both are the same shape of problem — pairwise "these two are the
same group" verdicts that have to be made transitive.
"""


class DisjointSet:
    def __init__(self):
        self._parent = {}
        self._rank = {}

    def add(self, key):
        if key not in self._parent:
            self._parent[key] = key
            self._rank[key] = 0
        return self

    def find(self, key):
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        # Path compression: point everything on the way up straight at the root.
        cursor = key
        while self._parent[cursor] != root:
            nxt = self._parent[cursor]
            self._parent[cursor] = root
            cursor = nxt
        return root

    def union(self, a, b):
        """Returns True if this call actually joined two distinct sets."""
        root_a = self.find(a)
        root_b = self.find(b)
        if root_a == root_b:
            return False
        rank_a = self._rank[root_a]
        rank_b = self._rank[root_b]
        if rank_a < rank_b:
            self._parent[root_a] = root_b
        elif rank_a > rank_b:
            self._parent[root_b] = root_a
        else:
            self._parent[root_b] = root_a
            self._rank[root_a] = rank_a + 1
        return True

    def connected(self, a, b):
        return self.find(a) == self.find(b)

    def groups(self):
        """Groups as lists of member keys, insertion-ordered within each group."""
        by_root = {}
        for key in list(self._parent):
            by_root.setdefault(self.find(key), []).append(key)
        return list(by_root.values())
