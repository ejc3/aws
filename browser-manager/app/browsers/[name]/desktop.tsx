"use client";

import { useEffect, useRef, useState } from "react";
import type RFB from "@novnc/novnc";
import type { BrowserInstance } from "../../../lib/contracts";
import { Icon } from "../../ui";
import { createReconnectLoop } from "../../../lib/reconnect.mjs";
import { watchVncQuality } from "../../../lib/vnc-quality.mjs";
import { createNavigationState } from "../../../lib/navigation-state.mjs";
import { frameViewport } from "../../../lib/frame-viewport.mjs";
import { canFitViewport } from "../../../lib/fit-viewport.mjs";
import { attachTouchScroll } from "../../../lib/touch-scroll.mjs";

type Connection = "connecting" | "connected" | "disconnected" | "error";
const keys = { enter: 0xff0d, tab: 0xff09, escape: 0xff1b, backspace: 0xff08, control: 0xffe3, alt: 0xffe9, left: 0xff51 };
const readFrameViewport = (target: HTMLDivElement | null) => {
  const canvas = target?.querySelector("canvas");
  return frameViewport(canvas?.width, canvas?.height);
};

export default function Desktop({ name }: { name: string }) {
  const screen = useRef<HTMLDivElement>(null);
  const workspace = useRef<HTMLElement>(null);
  const client = useRef<RFB | null>(null);
  const reconnect = useRef<() => void>(() => {});
  const textInput = useRef<HTMLTextAreaElement>(null);
  const viewportRequest = useRef<AbortController | null>(null);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [detail, setDetail] = useState("");
  const [retryDelay, setRetryDelay] = useState<number | null>(null);
  const [foreground, setForeground] = useState(true);
  const [fit, setFit] = useState(true);
  const [canFit, setCanFit] = useState(false);
  const [keyboard, setKeyboard] = useState(false);
  const [text, setText] = useState("");
  const [feedback, setFeedback] = useState("");
  const [fullscreen, setFullscreen] = useState(false);
  const [canFullscreen, setCanFullscreen] = useState(false);
  const [canGoBack, setCanGoBack] = useState<boolean | null>(null);
  const [label, setLabel] = useState(name);
  const [viewport, setViewport] = useState<BrowserInstance["viewport"] | null>(null);
  const [frame, setFrame] = useState<BrowserInstance["viewport"] | null>(null);
  const [viewportPending, setViewportPending] = useState(false);
  const [metadataError, setMetadataError] = useState("");
  const [viewportError, setViewportError] = useState("");
  const [overlayVisible, setOverlayVisible] = useState(true);
  const everConnected = useRef(false);
  const connected = connection === "connected";
  const currentViewport = frame ?? viewport;
  const phoneMode = currentViewport?.mode === "phone";

  useEffect(() => {
    if (!connected || !foreground || !screen.current) return;
    return attachTouchScroll(screen.current);
  }, [connected, foreground, fit, frame?.width, frame?.height]);

  // Hold the reconnect interstitial back for a few seconds so a brief drop (tab wake, network
  // blip) that reconnects quickly never flashes the overlay over the last frame. The first
  // connect and a hard auth error still show immediately.
  useEffect(() => {
    if (connected) { everConnected.current = true; setOverlayVisible(false); return; }
    if (connection === "error" || !everConnected.current) { setOverlayVisible(true); return; }
    const grace = setTimeout(() => setOverlayVisible(true), 5000);
    return () => clearTimeout(grace);
  }, [connected, connection]);

  useEffect(() => {
    const target = screen.current;
    const container = target?.parentElement;
    if (!target || !container) return;
    const changed = () => {
      const logical = readFrameViewport(target);
      setFrame(logical);
      const bounds = container.getBoundingClientRect();
      setCanFit(canFitViewport(logical?.width, logical?.height, bounds.width, bounds.height));
    };
    // Shared resizes arrive over VNC before (and independently of) metadata requests.
    // Observe only the actual framebuffer; fitting/scaling this viewer changes CSS, not these attributes.
    const observer = new MutationObserver(changed);
    observer.observe(target, { childList: true, subtree: true, attributes: true, attributeFilter: ["width", "height"] });
    const resize = new ResizeObserver(changed);
    resize.observe(container);
    changed();
    return () => { observer.disconnect(); resize.disconnect(); };
  }, [name]);

  useEffect(() => {
    const navigation = createNavigationState(async (signal: AbortSignal) => {
      const response = await fetch(`/api/browsers/${encodeURIComponent(name)}/navigation`, {
        cache: "no-store", signal,
      });
      if (!response.ok) throw new Error("Navigation state unavailable");
      const state = await response.json();
      return typeof state?.canGoBack === "boolean" ? state.canGoBack : null;
    }, setCanGoBack);
    navigation.setActive(connected && foreground);
    return () => navigation.dispose();
  }, [name, connected, foreground]);

  useEffect(() => {
    setLabel(name);
    setViewport(null);
    setFrame(null);
    setViewportError("");
    setMetadataError("");
    setViewportPending(false);
    return () => { viewportRequest.current?.abort(); viewportRequest.current = null; };
  }, [name]);

  useEffect(() => {
    if (!connected || viewportPending) return;
    const abort = new AbortController();
    setMetadataError("");
    void fetch("/api/browsers", { cache: "no-store", signal: abort.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not read the display mode. Reconnect to retry, or reload to renew your sign-in.");
        const data: { browsers: BrowserInstance[] } = await response.json();
        const browser = data.browsers.find((entry) => entry.name === name);
        if (!browser) throw new Error("This browser is no longer available.");
        if (!abort.signal.aborted) { setLabel(browser.label ?? name); setViewport(browser.viewport); }
      }).catch((error) => {
        if (!abort.signal.aborted) { setViewport(null); setMetadataError(error instanceof Error ? error.message : "Could not read the display mode. Reconnect to retry."); }
      });
    return () => abort.abort();
  }, [name, connected, viewportPending]);

  useEffect(() => {
    let disposed = false;
    let rfb: RFB | undefined;
    let stopQuality: () => void = () => {};
    let generation = 0;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    const isActive = () => document.visibilityState === "visible" && document.hasFocus();
    const loop = createReconnectLoop(connect, setRetryDelay, isActive());
    reconnect.current = loop.retry;

    function connect() {
      if (!isActive()) { loop.setActive(false); setForeground(false); return; }
      const attempt = ++generation;
      clearTimeout(timeout);
      stopQuality();
      rfb?.disconnect();
      rfb = undefined;
      client.current = null;
      setConnection("connecting");
      setDetail("");
      const current = () => !disposed && generation === attempt;
      const failed = (message: string, error = false) => {
        if (!current()) return;
        clearTimeout(timeout);
        stopQuality();
        setConnection((state) => error || state === "error" ? "error" : "disconnected");
        setDetail((detail) => detail || message);
        loop.disconnected();
      };
      // A half-open socket or stalled handshake must not leave a phone connecting forever.
      timeout = setTimeout(() => {
        if (!current()) return;
        failed("The connection timed out. Check that the desktop is running, or reload to renew your sign-in.");
        generation++;
        rfb?.disconnect();
      }, 10_000);
      void import("@novnc/novnc").then(({ default: RFBClient }) => {
        if (!current() || !screen.current || !isActive()) return;
        const url = new URL(`/browsers/${encodeURIComponent(name)}/vnc`, window.location.href);
        url.protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
        const next = new RFBClient(screen.current, url.href, { shared: true });
        rfb = next;
        stopQuality = watchVncQuality(next);
        // Keep noVNC responsible for both rendering and pointer scaling. The target fills the
        // viewer for Fit, or is sized to logical CSS pixels for natural-size HiDPI rendering.
        next.scaleViewport = true;
        // Scaling is local: one viewer must not resize another viewer's desktop.
        next.resizeSession = false;
        next.background = "#171d25";
        next.focusOnClick = true;
        client.current = next;
        next.addEventListener("connect", () => {
          if (!current()) return;
          clearTimeout(timeout);
          loop.connected();
          setConnection("connected");
          setDetail("");
        });
        next.addEventListener("disconnect", (event) => {
          const clean = (event as CustomEvent<{ clean: boolean }>).detail.clean;
          failed(clean
            ? "The desktop connection closed. Your browser may still be running."
            : "Could not reach this desktop. Check that it is running, or reload to renew your sign-in.");
        });
        const authenticationFailure = () => {
          if (!current()) return;
          failed("The desktop rejected the connection. Reload to renew your sign-in, or check the desktop status.", true);
          generation++;
          next.disconnect();
        };
        next.addEventListener("credentialsrequired", authenticationFailure);
        next.addEventListener("securityfailure", authenticationFailure);
      }).catch(() => failed("The desktop viewer could not load. Reload this page to try again.", true));
    }

    const activityChanged = () => {
      const active = isActive();
      setForeground(active);
      loop.setActive(active);
    };
    const suspended = () => { setForeground(false); loop.setActive(false); };
    document.addEventListener("visibilitychange", activityChanged);
    window.addEventListener("focus", activityChanged);
    window.addEventListener("blur", activityChanged);
    window.addEventListener("pageshow", activityChanged);
    window.addEventListener("pagehide", suspended);
    setForeground(isActive());
    loop.retry();
    return () => {
      disposed = true;
      generation++;
      clearTimeout(timeout);
      stopQuality();
      loop.dispose();
      reconnect.current = () => {};
      document.removeEventListener("visibilitychange", activityChanged);
      window.removeEventListener("focus", activityChanged);
      window.removeEventListener("blur", activityChanged);
      window.removeEventListener("pageshow", activityChanged);
      window.removeEventListener("pagehide", suspended);
      client.current = null;
      rfb?.disconnect();
    };
  }, [name]);

  useEffect(() => {
    setCanFullscreen(Boolean(document.fullscreenEnabled && workspace.current?.requestFullscreen));
    const changed = () => setFullscreen(document.fullscreenElement === workspace.current);
    document.addEventListener("fullscreenchange", changed);
    return () => document.removeEventListener("fullscreenchange", changed);
  }, []);
  useEffect(() => {
    if (keyboard) { client.current?.blur(); textInput.current?.focus(); }
  }, [keyboard, connection]);

  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else await workspace.current?.requestFullscreen();
    } catch { setFeedback("Fullscreen is unavailable here. Fit to screen still works."); }
  }

  async function togglePhoneMode() {
    // Read again at click time, so even a queued React update cannot repeat the already-active mode.
    const current = readFrameViewport(screen.current) ?? viewport;
    if (!connected || !current || viewportRequest.current) return;
    const abort = new AbortController();
    viewportRequest.current = abort;
    setViewportPending(true);
    setViewportError("");
    const bounds = screen.current?.parentElement?.getBoundingClientRect();
    const mobile = window.innerWidth <= 700;
    const requested = current.mode === "phone" ? { mode: "desktop" } : {
      mode: "phone",
      width: mobile ? Math.max(320, Math.min(500, Math.round(bounds?.width ?? window.innerWidth))) : 390,
      height: mobile ? Math.max(480, Math.min(900, Math.round(bounds?.height ?? 844))) : 844,
    };
    try {
      const response = await fetch(`/api/browsers/${encodeURIComponent(name)}/viewport`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify(requested), signal: abort.signal,
      });
      if (!response.ok) {
        const data = await response.json().catch(() => null);
        throw new Error(data?.error || "Could not change the display mode. Try again, or reload to renew your sign-in.");
      }
      const data: { browser: BrowserInstance } = await response.json();
      if (abort.signal.aborted) return;
      setViewport(data.browser.viewport);
      if (data.browser.viewport.mode === "phone") setFit(true);
      setFeedback(data.browser.viewport.mode === "phone"
        ? "Phone mode is shared with all viewers. Tap Phone to restore Desktop."
        : "Desktop restored for all viewers.");
    } catch (error) {
      if (!abort.signal.aborted) setViewportError(error instanceof Error ? error.message : "Could not change the display mode. Try again.");
    } finally {
      if (viewportRequest.current === abort) { viewportRequest.current = null; setViewportPending(false); }
    }
  }

  function sendKey(key: number, code: string) {
    if (!connected) return;
    client.current?.sendKey(key, code);
    setFeedback("Key sent to the remote desktop.");
  }

  function shortcut(letter: "l" | "v") {
    const rfb = client.current;
    if (!connected || !rfb) return;
    rfb.sendKey(keys.control, "ControlLeft", true);
    try { rfb.sendKey(letter.charCodeAt(0), `Key${letter.toUpperCase()}`); }
    finally { rfb.sendKey(keys.control, "ControlLeft", false); }
  }

  function browserBack() {
    const rfb = client.current;
    if (!connected || canGoBack !== true || !rfb) return;
    rfb.sendKey(keys.alt, "AltLeft", true);
    try { rfb.sendKey(keys.left, "ArrowLeft"); }
    finally { rfb.sendKey(keys.alt, "AltLeft", false); }
    setFeedback("Back sent to the remote browser.");
  }

  function sendText(paste: boolean) {
    const rfb = client.current;
    if (!connected || !rfb || !text) return;
    if (paste) {
      rfb.clipboardPasteFrom(text);
      shortcut("v");
    } else {
      for (const character of text.replace(/\r\n?/g, "\n")) {
        if (character === "\n") rfb.sendKey(keys.enter, "Enter");
        else if (character === "\t") rfb.sendKey(keys.tab, "Tab");
        else {
          // RFB uses Latin-1 keysyms directly and X11's Unicode keysym range beyond it.
          const point = character.codePointAt(0)!;
          rfb.sendKey(point >= 0x20 && point <= 0xff ? point : 0x01000000 | point, null);
        }
      }
    }
    setText("");
    setFeedback(paste ? "Paste sent to the selected remote field." : "Text sent to the selected remote field.");
    textInput.current?.focus();
  }

  return (
    <main className="desktop-workspace" ref={workspace}>
      <header className="desktop-header">
        <a href="/" className="icon-button back-button" aria-label="Back to your browsers"><Icon name="back" /></a>
        <div className="desktop-title"><h1>{label}</h1><span className="connection-status" data-state={connection} role="status"><span />{connected ? "Connected" : connection === "connecting" ? "Connecting…" : "Disconnected"}</span></div>
        <button className="button phone-mode" aria-label="Phone mode" aria-pressed={phoneMode} aria-describedby="phone-mode-help" disabled={!connected || viewportPending || !currentViewport} onClick={() => void togglePhoneMode()} title={phoneMode ? "Restore Desktop for all viewers" : "Use a phone-sized display for all viewers"}><Icon name="phone" size={19} /><span>{viewportPending ? "Changing…" : "Phone"}</span></button>
        <span id="phone-mode-help" className="sr-only">Changes the shared display for all viewers. Turn Phone mode off to restore Desktop.</span>
        <button className="button remote-back" disabled={!connected || canGoBack !== true} onClick={browserBack} aria-label="Back in remote browser" title={!connected ? "Connect to use browser Back" : canGoBack === false ? "No previous page in remote browser" : canGoBack === null ? "Checking remote browser history" : "Back in remote browser"}><Icon name="browserBack" size={20} /><span className="remote-back-label">Back</span></button>
        <div className="desktop-toolbar" aria-label="Desktop controls">
          <button className="button quiet" aria-label="Fit to screen" aria-pressed={fit} disabled={!connected || !canFit} onClick={() => setFit(!fit)} title={!connected ? "Connect to use Fit" : !canFit ? "Already at actual size" : fit ? "Show desktop at actual size" : "Fit desktop to screen"}><Icon name="fit" size={18} /><span>Fit<span className="fit-label-extra"> to screen</span></span></button>
          <button className="button quiet" aria-pressed={keyboard} aria-controls="keyboard-panel" onClick={() => setKeyboard(!keyboard)}><Icon name="keyboard" size={18} /><span>Keyboard</span></button>
          {canFullscreen && <button className="button quiet" onClick={() => void toggleFullscreen()} title="Toggle fullscreen" aria-label={fullscreen ? "Exit fullscreen" : "Enter fullscreen"}><Icon name="expand" size={18} /><span className="fullscreen-label">{fullscreen ? "Exit fullscreen" : "Fullscreen"}</span></button>}
          <button className="button quiet" onClick={() => reconnect.current()} disabled={connection === "connecting"} aria-label="Reconnect desktop"><Icon name="refresh" size={18} /><span>Reconnect</span></button>
        </div>
      </header>
      {(viewportError || metadataError) && <p className="viewport-error" role="alert">{viewportError || metadataError}</p>}

      <section className="desktop-display" aria-label={`${label} remote desktop`}>
        <div ref={screen} className="vnc-screen" style={!fit && currentViewport ? { width: currentViewport.width, height: currentViewport.height } : undefined} />
        {!connected && overlayVisible && <div className={`connection-overlay${keyboard ? " compact" : ""}`}><div className="connection-card">
          {connection === "connecting" ? <span className="spinner" aria-hidden="true" /> : <Icon name="browser" size={32} />}
          <h2>{connection === "connecting" ? "Connecting to your desktop" : "Desktop disconnected"}</h2>
          <p>{detail || "Opening a private connection to your browser."}</p>
          <p role="status">{!foreground ? "Automatic reconnect is paused until you return to this tab." : retryDelay !== null ? `Retrying automatically in ${retryDelay} ${retryDelay === 1 ? "second" : "seconds"}…` : ""}</p>
          {connection !== "connecting" && <div className="connection-actions"><button className="button primary" onClick={() => reconnect.current()}>Reconnect</button><a className="button" href="/">Your browsers</a></div>}
        </div></div>}
      </section>

      {keyboard && <section className="keyboard-panel" id="keyboard-panel" aria-labelledby="keyboard-heading">
        <div className="keyboard-heading"><div><h2 id="keyboard-heading">Send to desktop</h2><p>Select a field in the desktop first. Text is sent only when you choose an action.</p></div><button className="icon-button" onClick={() => setKeyboard(false)} aria-label="Close keyboard controls"><Icon name="close" size={18} /></button></div>
        <div className="keyboard-input-row"><label className="sr-only" htmlFor="remote-text">Text for the remote desktop</label><textarea id="remote-text" ref={textInput} value={text} onChange={(event) => setText(event.target.value)} onFocus={() => client.current?.blur()} placeholder="Type or paste text here…" autoCapitalize="none" autoCorrect="off" spellCheck={false} rows={2} maxLength={4096} /><div className="text-actions"><button className="button primary" disabled={!connected || !text} onClick={() => sendText(false)}>Type text</button><button className="button" disabled={!connected || !text} onClick={() => sendText(true)}>Paste text</button></div></div>
        <div className="keyboard-bottom"><div className="special-keys" aria-label="Remote keyboard shortcuts"><button className="button key" disabled={!connected} onClick={() => { shortcut("l"); setFeedback("Address bar selected in the remote browser."); }}>Address bar</button><button className="button key" disabled={!connected} onClick={() => sendKey(keys.tab, "Tab")}>Tab</button><button className="button key" disabled={!connected} onClick={() => sendKey(keys.escape, "Escape")}>Esc</button><button className="button key" disabled={!connected} onClick={() => sendKey(keys.backspace, "Backspace")}>Backspace</button><button className="button key" disabled={!connected} onClick={() => sendKey(keys.enter, "Enter")}>Enter ↵</button></div><p className="input-note">Paste updates the remote clipboard. New lines in typed text send Enter.</p></div>
      </section>}
      <footer className="desktop-footer"><span role="status">{feedback || "Swipe to scroll. To drag, tap once, then touch and drag."}</span><span className="desktop-footer-private"><Icon name="lock" size={12} /> Private connection</span></footer>
    </main>
  );
}
