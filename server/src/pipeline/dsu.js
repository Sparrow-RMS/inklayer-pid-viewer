/**
 * Disjoint-set union over string keys.
 *
 * Used twice in this pipeline: step 1 merges the duplicate sightings of one
 * entity across overlapping tiles, and step 3 clusters related processes
 * into HAZOP nodes. Both are the same shape of problem — pairwise "these two
 * are the same group" verdicts that have to be made transitive.
 */
export class DisjointSet {
  #parent = new Map()
  #rank = new Map()

  add(key) {
    if (!this.#parent.has(key)) {
      this.#parent.set(key, key)
      this.#rank.set(key, 0)
    }
    return this
  }

  find(key) {
    this.add(key)
    let root = key
    while (this.#parent.get(root) !== root) root = this.#parent.get(root)
    // Path compression: point everything on the way up straight at the root.
    let cursor = key
    while (this.#parent.get(cursor) !== root) {
      const next = this.#parent.get(cursor)
      this.#parent.set(cursor, root)
      cursor = next
    }
    return root
  }

  /** Returns true if this call actually joined two distinct sets. */
  union(a, b) {
    const rootA = this.find(a)
    const rootB = this.find(b)
    if (rootA === rootB) return false
    const rankA = this.#rank.get(rootA)
    const rankB = this.#rank.get(rootB)
    if (rankA < rankB) this.#parent.set(rootA, rootB)
    else if (rankA > rankB) this.#parent.set(rootB, rootA)
    else {
      this.#parent.set(rootB, rootA)
      this.#rank.set(rootA, rankA + 1)
    }
    return true
  }

  connected(a, b) {
    return this.find(a) === this.find(b)
  }

  /** Groups as arrays of member keys, insertion-ordered within each group. */
  groups() {
    const byRoot = new Map()
    for (const key of this.#parent.keys()) {
      const root = this.find(key)
      const list = byRoot.get(root)
      if (list) list.push(key)
      else byRoot.set(root, [key])
    }
    return [...byRoot.values()]
  }
}
