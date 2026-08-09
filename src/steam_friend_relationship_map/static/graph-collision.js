(function (globalScope, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  if (globalScope) {
    globalScope.GraphCollisionController = api.GraphCollisionController;
    globalScope.separateGraphCircles = api.separateGraphCircles;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const EPSILON = 0.01;

  function finiteNumber(value, fallback = 0) {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }

  function deterministicDirection(firstId, secondId) {
    const text = `${firstId}|${secondId}`;
    let hash = 2166136261;
    for (let index = 0; index < text.length; index += 1) {
      hash ^= text.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    const angle = ((hash >>> 0) / 0xffffffff) * Math.PI * 2;
    return { x: Math.cos(angle), y: Math.sin(angle) };
  }

  class CollisionSpace {
    constructor(circles = [], gap = 0) {
      this.entries = new Map();
      this.cells = new Map();
      this.gap = Math.max(0, finiteNumber(gap));
      this.cellSize = 32;
      this.rebuild(circles, this.gap);
    }

    rebuild(circles, gap = this.gap) {
      this.entries.clear();
      this.cells.clear();
      this.gap = Math.max(0, finiteNumber(gap));
      const normalized = Array.from(circles, (circle) => ({
        id: String(circle.id),
        x: finiteNumber(circle.x),
        y: finiteNumber(circle.y),
        radius: Math.max(0, finiteNumber(circle.radius)),
        fixed: Boolean(circle.fixed),
        item: circle.item || null,
      }));
      const maxRadius = normalized.reduce((maximum, circle) => Math.max(maximum, circle.radius), 0);
      this.cellSize = Math.max(32, maxRadius * 2 + this.gap + 1);
      for (const entry of normalized) this._insert(entry);
    }

    _cellCoordinates(x, y) {
      return {
        x: Math.floor(x / this.cellSize),
        y: Math.floor(y / this.cellSize),
      };
    }

    _cellKey(x, y) {
      return `${x}:${y}`;
    }

    _insert(entry) {
      const cell = this._cellCoordinates(entry.x, entry.y);
      entry.cellX = cell.x;
      entry.cellY = cell.y;
      this.entries.set(entry.id, entry);
      const key = this._cellKey(cell.x, cell.y);
      if (!this.cells.has(key)) this.cells.set(key, new Set());
      this.cells.get(key).add(entry.id);
    }

    _removeFromCell(entry) {
      const key = this._cellKey(entry.cellX, entry.cellY);
      const cell = this.cells.get(key);
      if (!cell) return;
      cell.delete(entry.id);
      if (!cell.size) this.cells.delete(key);
    }

    update(id, values) {
      const entry = this.entries.get(String(id));
      if (!entry) return null;
      this._removeFromCell(entry);
      entry.x = finiteNumber(values.x, entry.x);
      entry.y = finiteNumber(values.y, entry.y);
      entry.radius = Math.max(0, finiteNumber(values.radius, entry.radius));
      const cell = this._cellCoordinates(entry.x, entry.y);
      entry.cellX = cell.x;
      entry.cellY = cell.y;
      const key = this._cellKey(cell.x, cell.y);
      if (!this.cells.has(key)) this.cells.set(key, new Set());
      this.cells.get(key).add(entry.id);
      return entry;
    }

    _neighbors(entry) {
      const ids = new Set();
      for (let x = entry.cellX - 1; x <= entry.cellX + 1; x += 1) {
        for (let y = entry.cellY - 1; y <= entry.cellY + 1; y += 1) {
          const cell = this.cells.get(this._cellKey(x, y));
          if (cell) for (const id of cell) ids.add(id);
        }
      }
      ids.delete(entry.id);
      return [...ids].sort().map((id) => this.entries.get(id)).filter(Boolean);
    }

    _isFixed(entry, anchorId) {
      if (entry.id === anchorId || entry.fixed) return true;
      if (entry.item?.locked?.()) return true;
      return Boolean(entry.item?.grabbed?.());
    }

    resolveFrom(anchorId, maxMoves = 500) {
      const normalizedAnchor = String(anchorId);
      const anchor = this.entries.get(normalizedAnchor);
      const positions = new Map();
      if (!anchor) return { moves: 0, exhausted: false, positions };

      const queue = [anchor];
      let cursor = 0;
      let moves = 0;
      while (cursor < queue.length && moves < maxMoves) {
        const current = queue[cursor];
        cursor += 1;
        for (const other of this._neighbors(current)) {
          if (moves >= maxMoves) break;
          let dx = other.x - current.x;
          let dy = other.y - current.y;
          const minimumDistance = current.radius + other.radius + this.gap;
          const distanceSquared = dx * dx + dy * dy;
          if (distanceSquared + EPSILON >= minimumDistance * minimumDistance) continue;

          let distance = Math.sqrt(Math.max(0, distanceSquared));
          let unitX;
          let unitY;
          if (distance < EPSILON) {
            const direction = deterministicDirection(current.id, other.id);
            unitX = direction.x;
            unitY = direction.y;
            distance = 0;
          } else {
            unitX = dx / distance;
            unitY = dy / distance;
          }
          const overlap = minimumDistance - distance + EPSILON;
          const currentFixed = this._isFixed(current, normalizedAnchor);
          const otherFixed = this._isFixed(other, normalizedAnchor);
          if (currentFixed && otherFixed) continue;

          const moved = otherFixed ? current : other;
          const direction = otherFixed ? -1 : 1;
          this.update(moved.id, {
            x: moved.x + unitX * overlap * direction,
            y: moved.y + unitY * overlap * direction,
          });
          positions.set(moved.id, { x: moved.x, y: moved.y });
          queue.push(moved);
          moves += 1;
        }
      }
      return { moves, exhausted: cursor < queue.length, positions };
    }

    resolveAll(maxPasses = 4, maxMoves = 5000) {
      const positions = new Map();
      let totalMoves = 0;
      for (let pass = 0; pass < maxPasses && totalMoves < maxMoves; pass += 1) {
        let passMoves = 0;
        for (const id of [...this.entries.keys()].sort()) {
          const result = this.resolveFrom(id, Math.max(1, maxMoves - totalMoves));
          passMoves += result.moves;
          totalMoves += result.moves;
          for (const [movedId, position] of result.positions) positions.set(movedId, position);
          if (totalMoves >= maxMoves) break;
        }
        if (!passMoves) break;
      }
      return { moves: totalMoves, exhausted: totalMoves >= maxMoves, positions };
    }
  }

  function separateGraphCircles(circles, options = {}) {
    const clones = circles.map((circle) => ({ ...circle }));
    const space = new CollisionSpace(clones, options.gap || 0);
    const result = options.anchorId === undefined
      ? space.resolveAll(options.maxPasses || 4, options.maxMoves || 5000)
      : space.resolveFrom(options.anchorId, options.maxMoves || 5000);
    return {
      ...result,
      circles: [...space.entries.values()].map(({ id, x, y, radius, fixed }) => ({
        id,
        x,
        y,
        radius,
        fixed,
      })),
    };
  }

  class GraphCollisionController {
    constructor(cy, options = {}) {
      this.cy = cy;
      this.gap = Math.max(0, finiteNumber(options.gap, 12));
      this.maxMovesPerFrame = Math.max(50, finiteNumber(options.maxMovesPerFrame, 500));
      this.requestFrame = options.requestAnimationFrame
        || globalThis.requestAnimationFrame?.bind(globalThis)
        || ((callback) => setTimeout(callback, 16));
      this.cancelFrame = options.cancelAnimationFrame
        || globalThis.cancelAnimationFrame?.bind(globalThis)
        || clearTimeout;
      this.space = new CollisionSpace([], this.gap);
      this.frameId = null;
      this.pendingAnchorId = null;
      this.bound = false;
      this.onDrag = (event) => this.schedule(event.target.id());
      this.onFree = (event) => this.settleFrom(event.target.id());
      this.onLayoutStop = () => this.settleAll();
    }

    bind() {
      if (this.bound) return this;
      this.bound = true;
      this.cy.on("drag", "node", this.onDrag);
      this.cy.on("free", "node", this.onFree);
      this.cy.on("layoutstop", this.onLayoutStop);
      this.refresh();
      return this;
    }

    circles() {
      return this.cy.nodes().map((node) => {
        const position = node.position();
        return {
          id: node.id(),
          x: position.x,
          y: position.y,
          radius: Math.max(node.outerWidth(), node.outerHeight()) / 2,
          fixed: node.locked(),
          item: node,
        };
      });
    }

    refresh() {
      this.space.rebuild(this.circles(), this.gap);
    }

    setGap(gap, settle = false) {
      this.gap = Math.max(0, finiteNumber(gap, this.gap));
      this.refresh();
      if (settle) this.settleAll();
    }

    applyPositions(positions, anchorId = null) {
      if (!positions.size) return;
      this.cy.batch(() => {
        for (const [id, position] of positions) {
          if (id === anchorId) continue;
          const node = this.cy.getElementById(id);
          if (node.empty() || node.locked() || node.grabbed()) continue;
          node.position(position);
        }
      });
    }

    updateNodeInSpace(node) {
      const position = node.position();
      this.space.update(node.id(), {
        x: position.x,
        y: position.y,
        radius: Math.max(node.outerWidth(), node.outerHeight()) / 2,
      });
    }

    schedule(anchorId) {
      this.pendingAnchorId = String(anchorId);
      if (this.frameId !== null) return;
      this.frameId = this.requestFrame(() => {
        this.frameId = null;
        const currentAnchor = this.pendingAnchorId;
        this.pendingAnchorId = null;
        const node = this.cy.getElementById(currentAnchor);
        if (node.empty()) return;
        this.updateNodeInSpace(node);
        const result = this.space.resolveFrom(currentAnchor, this.maxMovesPerFrame);
        this.applyPositions(result.positions, currentAnchor);
        if (result.exhausted) this.schedule(currentAnchor);
      });
    }

    settleFrom(anchorId) {
      if (this.frameId !== null) {
        this.cancelFrame(this.frameId);
        this.frameId = null;
      }
      this.refresh();
      const result = this.space.resolveFrom(anchorId, this.maxMovesPerFrame * 8);
      this.applyPositions(result.positions, String(anchorId));
      this.refresh();
    }

    settleAll() {
      this.refresh();
      const result = this.space.resolveAll(4, this.maxMovesPerFrame * 12);
      this.applyPositions(result.positions);
      this.refresh();
    }

    destroy() {
      if (!this.bound) return;
      this.bound = false;
      if (this.frameId !== null) this.cancelFrame(this.frameId);
      this.frameId = null;
      this.cy.off("drag", "node", this.onDrag);
      this.cy.off("free", "node", this.onFree);
      this.cy.off("layoutstop", this.onLayoutStop);
    }
  }

  return { CollisionSpace, GraphCollisionController, separateGraphCircles };
});
