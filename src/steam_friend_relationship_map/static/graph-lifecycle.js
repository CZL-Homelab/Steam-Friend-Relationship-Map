(function exposeGraphLifecycle(root, factory) {
  const exported = factory();
  if (typeof module === "object" && module.exports) module.exports = exported;
  if (root) root.GraphLifecycleCoordinator = exported.GraphLifecycleCoordinator;
})(typeof globalThis !== "undefined" ? globalThis : this, function createGraphLifecycle() {
  class GraphLifecycleCoordinator {
    constructor(options = {}) {
      this.setTimeout = options.setTimeout || globalThis.setTimeout.bind(globalThis);
      this.clearTimeout = options.clearTimeout || globalThis.clearTimeout.bind(globalThis);
      this.renderId = 0;
      this.chunkTimer = null;
      this.activeLayout = null;
    }

    beginRender() {
      this.renderId += 1;
      this.cancelChunk();
      this.stopLayout();
      return this.renderId;
    }

    isCurrent(renderId) {
      return renderId === this.renderId;
    }

    scheduleChunk(renderId, callback, delay = 0) {
      this.cancelChunk();
      if (!this.isCurrent(renderId)) return false;
      this.chunkTimer = this.setTimeout(() => {
        this.chunkTimer = null;
        if (this.isCurrent(renderId)) callback();
      }, delay);
      return true;
    }

    cancelChunk() {
      if (this.chunkTimer === null) return;
      this.clearTimeout(this.chunkTimer);
      this.chunkTimer = null;
    }

    startLayout(layout) {
      this.stopLayout();
      this.activeLayout = layout;
      if (layout && typeof layout.one === "function") {
        layout.one("layoutstop", () => {
          if (this.activeLayout === layout) this.activeLayout = null;
        });
      }
      if (layout && typeof layout.run === "function") layout.run();
      return layout;
    }

    stopLayout() {
      const layout = this.activeLayout;
      this.activeLayout = null;
      if (layout && typeof layout.stop === "function") layout.stop();
    }

    cancel() {
      this.renderId += 1;
      this.cancelChunk();
      this.stopLayout();
    }
  }

  return { GraphLifecycleCoordinator };
});
