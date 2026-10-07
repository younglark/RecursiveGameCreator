/* Public game contract. Inputs are delivered by Playwright, never simulated here. */
(() => {
  let target = null;
  window.GameAgentBridge = Object.freeze({
    get ready() { return target !== null; },
    register(game) {
      if (typeof game.reset !== 'function' || typeof game.observe !== 'function')
        throw new Error('GameAgentBridge needs reset(seed) and observe()');
      target = game;
    },
    async reset(seed) {
      if (!target) throw new Error('Game is not registered');
      await target.reset(seed);
    },
    async observe() {
      if (!target) throw new Error('Game is not registered');
      const value = await target.observe();
      if (!value || typeof value !== 'object' || Array.isArray(value))
        throw new Error('Public observation must be an object');
      return JSON.parse(JSON.stringify(value));
    }
  });
})();
