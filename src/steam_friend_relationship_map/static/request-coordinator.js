"use strict";

class LatestRequestCoordinator {
  constructor(AbortControllerClass = globalThis.AbortController) {
    if (typeof AbortControllerClass !== "function") {
      throw new Error("AbortController is required");
    }
    this.AbortControllerClass = AbortControllerClass;
    this.requests = new Map();
  }

  begin(key) {
    this.cancel(key);
    const entry = {
      controller: new this.AbortControllerClass(),
    };
    this.requests.set(key, entry);
    return {
      signal: entry.controller.signal,
      isCurrent: () => this.requests.get(key) === entry,
      finish: () => {
        if (this.requests.get(key) === entry) this.requests.delete(key);
      },
    };
  }

  cancel(key) {
    const entry = this.requests.get(key);
    if (!entry) return false;
    this.requests.delete(key);
    entry.controller.abort();
    return true;
  }

  cancelMany(keys) {
    for (const key of keys) this.cancel(key);
  }
}

if (typeof window !== "undefined") {
  window.LatestRequestCoordinator = LatestRequestCoordinator;
}
if (typeof module !== "undefined" && module.exports) {
  module.exports = { LatestRequestCoordinator };
}
