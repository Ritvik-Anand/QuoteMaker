// Minimal service worker — exists only to satisfy installability requirements
// (Chrome/Android require a registered SW to offer "Add to Home Screen").
// Deliberately does no caching: this app is login-gated and data-heavy, so
// serving stale HTML/API responses would do more harm than good. Every
// request just passes straight through to the network.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
