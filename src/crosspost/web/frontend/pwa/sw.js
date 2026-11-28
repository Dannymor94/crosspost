/* T-517: no authenticated response, media, or mutation is cached or replayed. */
"use strict";
const CACHE = "crosspost-public-offline-v1";
const OFFLINE = "/static/pwa/offline.html";
self.addEventListener("install", event => {
  event.waitUntil((async () => {
    const response = await fetch(OFFLINE, {credentials:"omit", cache:"no-store", redirect:"error"});
    if (!response.ok || !(response.headers.get("content-type") || "").includes("text/html")) {
      throw Error("Offline shell unavailable");
    }
    const cache = await caches.open(CACHE);
    await cache.put(OFFLINE, response);
  })());
});
self.addEventListener("activate", event => {
  event.waitUntil((async () => {
    for (const key of await caches.keys()) {
      if (key.startsWith("crosspost-public-offline-") && key !== CACHE) await caches.delete(key);
    }
    await self.clients.claim();
  })());
});
self.addEventListener("fetch", event => {
  const request = event.request;
  const url = new URL(request.url);
  // Only top-level app navigation can fall back. Do not intercept account,
  // API, signed media, static JS, cross-origin or non-GET traffic.
  if (request.method !== "GET" || request.mode !== "navigate" ||
      url.origin !== self.location.origin || url.search ||
      !["/", "/client"].includes(url.pathname)) return;
  event.respondWith((async () => {
    try {
      return await fetch(request, {cache:"no-store"});
    } catch (_) {
      const cache = await caches.open(CACHE);
      return (await cache.match(OFFLINE)) || Response.error();
    }
  })());
});
