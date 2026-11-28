import assert from "node:assert/strict";
import test from "node:test";
import { familyPublicPages } from "../workers/family-public-pages/worker.mjs";

const hostname = "family.dolphin-labs.dev";
const documents = {
  "/privacy/": "<!doctype html><h1>Privacy fixture</h1>",
  "/support/": "<!doctype html><h1>Support fixture</h1>",
};
const worker = familyPublicPages(hostname, documents);
const request = (path, method = "GET") => worker.fetch(new Request(new URL(path, `https://${hostname}`), { method }));

test("GET serves the approved document inline and HEAD returns its headers", async () => {
  for (const [path, document] of Object.entries(documents)) {
    const get = request(path + "?from=app-store");
    assert.equal(get.status, 200);
    assert.match(get.headers.get("Content-Type"), /text\/html/);
    assert.match(get.headers.get("Cache-Control"), /no-transform/);
    assert.match(get.headers.get("Content-Security-Policy"), /default-src 'none'/);
    assert.equal(await get.text(), document);
    const head = request(path, "HEAD");
    assert.equal(head.status, 200);
    assert.deepEqual([...head.headers], [...get.headers]);
    assert.equal(await head.text(), "");
  }
});

test("sibling names, descendants, private routes, assets and alternate hosts cannot serve a page", async () => {
  for (const path of [
    "/", "/api/health", "/board/", "/_next/static/chunks/main.js", "/privacy", "/support",
    "/privacy-settings/", "/supporter/", "/privacy/archive/", "/support/private", "/privacy/index.html",
    "/privacy%2F", "/Privacy/", "https://another.example/privacy/",
  ]) {
    const response = request(path);
    assert.equal(response.status, 404, path);
    assert.equal(await response.text(), "", path);
  }
});

test("only GET and HEAD are served", async () => {
  for (const method of ["POST", "PUT", "DELETE", "OPTIONS"]) {
    const response = request("/privacy/", method);
    assert.equal(response.status, 405);
    assert.equal(response.headers.get("Allow"), "GET, HEAD");
    assert.equal(await response.text(), "");
  }
});
