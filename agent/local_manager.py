"""Local GPU model lifecycle manager for Caduceus ``/local`` mode.

Caduceus can run workflow *workers* on models served locally on the user's GPU
(llama.cpp / any OpenAI-compatible server). A single GPU is an exclusive,
VRAM-bounded resource, so this module owns the bits Hermes otherwise lacks:

* **a source-of-truth manifest** — each local model's endpoint, served name,
  VRAM, max context, capability card, exclusivity group, load/unload/health
  hooks, and **serving profiles** (``slots × ctx-per-slot``);
* **a GPU state machine** — which ``(model, profile)`` is currently loaded;
* **serialized hot-swaps** — ``ensure()`` takes a lock, unloads any
  exclusivity-group conflict, runs the load hook, and polls health, so two
  models are never co-resident and a half-swapped state is never observed;
* **capacity reporting** — how many parallel slots a model can serve, which the
  Loom scheduler uses to size fan-out.

The orchestrator ("brain") stays on the user's cloud/session model — only
workers go local — so this manager is consulted from the workflow leaf path,
never for the main agent.

Pure standard library (``subprocess`` for hooks, ``urllib`` for health) so it
ships with the rest of Caduceus and adds no dependencies. The hook runner and
health checker are injectable for fully offline tests.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class LocalModelError(RuntimeError):
    """Raised when a local model can't be loaded / reached."""


# ---------------------------------------------------------------------------
# Manifest model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalProfile:
    """One serving configuration of a model: ``slots`` parallel requests, each
    with up to ``ctx`` context tokens. Switching profiles is a reload."""

    slots: int
    ctx: int
    picker: str = ""          # label passed to the load hook (env + arg)
    default: bool = False
    load: str = ""            # optional per-profile load override

    def key(self) -> str:
        return self.picker or f"{self.slots}x{self.ctx}"


@dataclass
class LocalModel:
    id: str
    endpoint: str                       # OpenAI-compatible base_url (…/v1)
    served_model_name: str              # the "model" string the server expects
    api_key: str = "local"              # placeholder when the server needs none
    api_mode: str = "chat_completions"
    provider: str = "custom"
    group: str = "gpu"                  # exclusivity group (co-load forbidden)
    vram_mb: int = 0
    max_context: int = 0
    card: str = ""                      # capability description (routing/orchestrator)
    cost: float = 0.0
    reasoning_split: bool = False
    load: str = ""
    unload: str = ""
    status_cmd: str = ""
    health: str = ""                    # health URL (else /models is probed)
    profiles: List[LocalProfile] = field(default_factory=list)

    def __post_init__(self):
        if not self.profiles:
            # A model with no declared profiles serves a single slot at its max
            # context (or a sane default).
            self.profiles = [LocalProfile(slots=1, ctx=self.max_context or 0, default=True)]

    def default_profile(self) -> LocalProfile:
        for p in self.profiles:
            if p.default:
                return p
        # Else the widest (most slots) profile.
        return max(self.profiles, key=lambda p: p.slots)

    def max_slots(self) -> int:
        return max((p.slots for p in self.profiles), default=1)

    def select_profile(self, *, want_ctx: int = 0, want_slots: int = 0) -> LocalProfile:
        """Pick the best serving profile for a leaf's needs.

        Among profiles whose per-slot context covers ``want_ctx``, prefer maximum
        parallelism (most slots). ``want_slots`` is an optional lower bound on
        parallelism (smallest profile that still meets it). If nothing covers the
        requested context, fall back to the **largest-context** profile (closest
        to satisfying it) — context is best-effort and never blocks scheduling.
        """
        viable = [p for p in self.profiles if (not want_ctx or p.ctx >= want_ctx)]
        if not viable:
            return max(self.profiles, key=lambda p: (p.ctx, p.slots))
        if want_slots:
            wide = [p for p in viable if p.slots >= want_slots]
            if wide:
                # smallest profile that still meets the slot floor (max ctx tiebreak)
                return min(wide, key=lambda p: (p.slots, -p.ctx))
        return max(viable, key=lambda p: (p.slots, p.ctx))


# ---------------------------------------------------------------------------
# Manifest parsing
# ---------------------------------------------------------------------------


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def parse_profiles(raw: Any) -> List[LocalProfile]:
    out: List[LocalProfile] = []
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        slots = max(1, _coerce_int(p.get("slots"), 1))
        ctx = max(0, _coerce_int(p.get("ctx"), 0))
        out.append(
            LocalProfile(
                slots=slots,
                ctx=ctx,
                picker=str(p.get("picker") or "").strip(),
                default=bool(p.get("default", False)),
                load=str(p.get("load") or "").strip(),
            )
        )
    return out


def parse_model(raw: Dict[str, Any]) -> Optional[LocalModel]:
    mid = str(raw.get("id") or "").strip()
    endpoint = str(raw.get("endpoint") or raw.get("base_url") or "").strip()
    if not mid or not endpoint:
        logger.warning("caduceus.local model skipped (needs id + endpoint): %r", raw)
        return None
    served = str(raw.get("served_model_name") or raw.get("model") or mid).strip()
    return LocalModel(
        id=mid,
        endpoint=endpoint,
        served_model_name=served,
        api_key=str(raw.get("api_key") or "local"),
        api_mode=str(raw.get("api_mode") or "chat_completions"),
        provider=str(raw.get("provider") or "custom"),
        group=str(raw.get("group") or "gpu"),
        vram_mb=_coerce_int(raw.get("vram_mb"), 0),
        max_context=_coerce_int(raw.get("max_context"), 0),
        card=str(raw.get("card") or "").strip(),
        cost=float(raw.get("cost") or 0.0),
        reasoning_split=bool(raw.get("reasoning_split", False)),
        load=str(raw.get("load") or "").strip(),
        unload=str(raw.get("unload") or "").strip(),
        status_cmd=str(raw.get("status") or "").strip(),
        health=str(raw.get("health") or "").strip(),
        profiles=parse_profiles(raw.get("profiles")),
    )


# ---------------------------------------------------------------------------
# Default hook runner + health checker (overridable for tests)
# ---------------------------------------------------------------------------


def _default_run_hook(cmd: str, env: Dict[str, str], timeout: float) -> Tuple[int, str]:
    """Run a shell hook; return (returncode, combined_output_tail)."""
    full_env = dict(os.environ)
    full_env.update(env or {})
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            env=full_env,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired:
        return 124, f"hook timed out after {timeout}s: {cmd}"
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode, out[-2000:]


def _default_health_ok(url: str, timeout: float = 4.0) -> bool:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", resp.getcode()) < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------


class LocalModelManager:
    """Owns GPU model lifecycle for ``/local`` mode. Thread-safe."""

    def __init__(
        self,
        cfg: Dict[str, Any],
        *,
        run_hook: Optional[Callable[[str, Dict[str, str], float], Tuple[int, str]]] = None,
        health_ok: Optional[Callable[[str, float], bool]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        cfg = cfg or {}
        self._models: Dict[str, LocalModel] = {}
        for raw in cfg.get("models") or []:
            if isinstance(raw, dict):
                m = parse_model(raw)
                if m:
                    self._models[m.id] = m

        self.default_worker_id: str = str(cfg.get("default_worker") or "").strip()
        if not self.default_worker_id and self._models:
            self.default_worker_id = next(iter(self._models))

        self.unload_on_off: bool = bool(cfg.get("unload_on_off", True))
        self.fallback_to_cloud: bool = bool(cfg.get("fallback_to_cloud", False))
        self.load_timeout: float = float(cfg.get("load_timeout_seconds") or 180)
        self.health_poll: float = float(cfg.get("health_poll_seconds") or 2)

        self._run_hook = run_hook or _default_run_hook
        self._health_ok = health_ok or _default_health_ok
        self._sleep = sleep or time.sleep

        # GPU state machine. (model_id, profile_key) or None.
        self._loaded: Optional[Tuple[str, str]] = None
        self._lock = threading.RLock()

    # ---- introspection ------------------------------------------------
    def has_models(self) -> bool:
        return bool(self._models)

    def models(self) -> List[LocalModel]:
        return list(self._models.values())

    def model(self, model_id: str) -> Optional[LocalModel]:
        return self._models.get(model_id)

    def is_local_id(self, value: Optional[str]) -> bool:
        if not value:
            return False
        v = value.strip()
        if v.startswith("local:"):
            v = v.split(":", 1)[1].strip()
        return v in self._models

    def normalize_id(self, value: str) -> str:
        v = (value or "").strip()
        if v.startswith("local:"):
            v = v.split(":", 1)[1].strip()
        return v

    def resolve_worker_id(self, model_hint: Optional[str], provider_hint: Optional[str] = None) -> Optional[str]:
        """Pick the local model id for a worker leaf, or ``None`` for the cloud path.

        Semantics under ``/local`` (workers-local-by-default):
          * a known local id (``local:<id>`` or bare) → that local model;
          * ``provider=local`` with an unknown id → the default local worker;
          * an explicitly-named **non-local** model/provider → ``None`` (the leaf
            escapes to the normal cloud path — lets the orchestrator pin a
            specific cloud worker);
          * an **untagged** leaf → the default local worker.
        """
        prov = (provider_hint or "").strip().lower()
        mid = self.normalize_id(model_hint) if model_hint else ""
        if mid and mid in self._models:
            return mid
        if prov == "local":
            # Explicit local intent but unknown id → default local worker.
            return self.default_worker_id or None
        if model_hint or prov:
            # An explicit non-local model/provider was requested → not ours.
            return None
        # Untagged leaf → default local worker.
        return self.default_worker_id or None

    def capacity(self, model_id: str) -> int:
        m = self._models.get(model_id)
        return m.max_slots() if m else 1

    def max_capacity(self) -> int:
        return max((m.max_slots() for m in self._models.values()), default=1)

    def creds(self, model_id: str) -> Dict[str, Any]:
        m = self._models[model_id]
        return {
            "base_url": m.endpoint,
            "api_key": m.api_key,
            "model": m.served_model_name,
            "provider": m.provider,
            "api_mode": m.api_mode,
        }

    def loaded(self) -> Optional[Tuple[str, str]]:
        return self._loaded

    def catalog(self) -> List[Dict[str, Any]]:
        """Compact list for surfacing to the orchestrator / status."""
        out = []
        for m in self._models.values():
            out.append(
                {
                    "id": m.id,
                    "card": m.card,
                    "max_context": m.max_context,
                    "max_slots": m.max_slots(),
                    "default": m.id == self.default_worker_id,
                }
            )
        return out

    # ---- lifecycle ----------------------------------------------------
    def _conflicts(self, model_id: str) -> List[str]:
        """Loaded models that can't coexist with ``model_id`` (same group)."""
        target = self._models.get(model_id)
        if not target or not self._loaded:
            return []
        loaded_id = self._loaded[0]
        if loaded_id == model_id:
            return []
        loaded = self._models.get(loaded_id)
        if loaded and loaded.group == target.group:
            return [loaded_id]
        return []

    def _run(self, cmd: str, *, env: Dict[str, str], what: str) -> None:
        if not cmd:
            return
        logger.info("local: %s -> %s", what, cmd)
        rc, out = self._run_hook(cmd, env, self.load_timeout)
        if rc != 0:
            raise LocalModelError(f"{what} failed (rc={rc}): {out or cmd}")

    def _profile_env(self, m: LocalModel, p: LocalProfile) -> Dict[str, str]:
        return {
            "LOCAL_MODEL_ID": m.id,
            "LOCAL_PROFILE_PICKER": p.picker,
            "LOCAL_PROFILE_SLOTS": str(p.slots),
            "LOCAL_PROFILE_CTX": str(p.ctx),
        }

    def _wait_health(self, m: LocalModel) -> None:
        url = m.health or (m.endpoint.rstrip("/") + "/models")
        deadline = time.monotonic() + self.load_timeout
        while time.monotonic() < deadline:
            if self._health_ok(url, 4.0):
                return
            self._sleep(self.health_poll)
        raise LocalModelError(
            f"local model {m.id!r} did not become healthy within "
            f"{self.load_timeout:.0f}s at {url}"
        )

    def ensure(self, model_id: str, *, want_ctx: int = 0, want_slots: int = 0) -> Dict[str, Any]:
        """Ensure ``model_id`` is loaded with a profile that fits, swapping if
        needed. Serialized; idempotent when already satisfied. Returns creds."""
        m = self._models.get(model_id)
        if not m:
            raise LocalModelError(f"unknown local model id: {model_id!r}")
        target = m.select_profile(want_ctx=want_ctx, want_slots=want_slots)
        target_key = target.key()
        with self._lock:
            if self._loaded == (model_id, target_key):
                return self.creds(model_id)
            # Unload exclusivity-group conflicts first.
            for cid in self._conflicts(model_id):
                cm = self._models[cid]
                self._run(cm.unload, env={"LOCAL_MODEL_ID": cid}, what=f"unload {cid}")
            # Reloading the same model under a different profile: unload first.
            if self._loaded and self._loaded[0] == model_id:
                self._run(m.unload, env={"LOCAL_MODEL_ID": model_id},
                          what=f"unload {model_id} (profile switch)")
            self._loaded = None
            load_cmd = target.load or m.load
            if not load_cmd:
                raise LocalModelError(
                    f"local model {model_id!r} has no load hook configured"
                )
            self._run(load_cmd, env=self._profile_env(m, target),
                      what=f"load {model_id} [{target_key}]")
            self._wait_health(m)
            self._loaded = (model_id, target_key)
            logger.info("local: %s [%s] ready (%d slots)", model_id, target_key, target.slots)
            return self.creds(model_id)

    def current_slots(self) -> int:
        """Parallel slots of the currently-loaded profile (0 if none loaded)."""
        if not self._loaded:
            return 0
        mid, key = self._loaded
        m = self._models.get(mid)
        if not m:
            return 0
        for p in m.profiles:
            if p.key() == key:
                return p.slots
        return 1

    def unload_all(self) -> None:
        with self._lock:
            if not self._loaded:
                return
            mid = self._loaded[0]
            m = self._models.get(mid)
            if m and m.unload:
                try:
                    self._run(m.unload, env={"LOCAL_MODEL_ID": mid}, what=f"unload {mid}")
                except LocalModelError as exc:
                    logger.warning("local: unload_all failed: %s", exc)
            self._loaded = None

    def status(self) -> Dict[str, Any]:
        loaded = None
        if self._loaded:
            loaded = {"model": self._loaded[0], "profile": self._loaded[1],
                      "slots": self.current_slots()}
        return {
            "models": self.catalog(),
            "default_worker": self.default_worker_id,
            "loaded": loaded,
            "fallback_to_cloud": self.fallback_to_cloud,
        }


# ---------------------------------------------------------------------------
# Process-wide singleton (the GPU is one resource)
# ---------------------------------------------------------------------------

_manager: Optional[LocalModelManager] = None
_manager_sig: Optional[int] = None
_singleton_lock = threading.Lock()


def _config_signature(cfg: Dict[str, Any]) -> int:
    import json

    try:
        return hash(json.dumps(cfg or {}, sort_keys=True, default=str))
    except Exception:
        return id(cfg)


def get_local_manager(cfg: Optional[Dict[str, Any]]) -> Optional[LocalModelManager]:
    """Return the process-wide manager for the given ``caduceus.local`` config.

    Rebuilt only when the config changes. Returns ``None`` when no local models
    are declared (so callers cleanly no-op).
    """
    global _manager, _manager_sig
    if not cfg:
        return _manager
    sig = _config_signature(cfg)
    with _singleton_lock:
        if _manager is None or sig != _manager_sig:
            mgr = LocalModelManager(cfg)
            if not mgr.has_models():
                _manager = None
                _manager_sig = sig
                return None
            # Carry over loaded state when rebuilding for an unchanged GPU.
            if _manager is not None:
                mgr._loaded = _manager._loaded  # noqa: SLF001
            _manager = mgr
            _manager_sig = sig
        return _manager


def reset_local_manager() -> None:
    """Drop the cached singleton (tests / hard reconfigure)."""
    global _manager, _manager_sig
    with _singleton_lock:
        _manager = None
        _manager_sig = None
