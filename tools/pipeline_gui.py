"""Desktop overview of the autoroad processing steps.

Usage:
  cd C:\\temp\\beamng_autoroad; python tools\\pipeline_gui.py
"""
from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pipeline_catalog import (  # noqa: E402
    CORE_STEP_IDS,
    STEPS,
    STEPS_BY_ID,
    Step,
    command_for,
    config_rows,
    in_site_sequence,
    is_applicable,
    last_run_label,
    list_sites,
    load_gui_state,
    record_run,
    save_gui_state,
)

DOC_BASE = ROOT


def open_path(path: Path) -> None:
    if not path.exists():
        messagebox.showwarning("Not found", f"{path}")
        return
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
        return
    webbrowser.open(path.resolve().as_uri())


class StepCard(ttk.Frame):
    def __init__(
        self,
        parent: tk.Widget,
        app: "PipelineApp",
        step: Step,
        number: int,
    ) -> None:
        super().__init__(parent, style="Card.TFrame", padding=(10, 8))
        self.app = app
        self.step = step
        self.number = number
        self.flag_vars: dict[str, tk.Variable] = {}

        head = ttk.Frame(self)
        head.pack(fill="x")
        ttk.Label(head, text=f"{number:02d}", style="Num.TLabel").pack(side="left")
        ttk.Label(head, text=step.title, style="Title.TLabel").pack(side="left", padx=(8, 4))
        if step.core:
            ttk.Label(head, text="core", style="Badge.TLabel").pack(side="left", padx=(4, 0))
        ttk.Button(
            head,
            text="?",
            width=3,
            command=lambda: open_path(DOC_BASE / step.docs),
        ).pack(side="right")
        ttk.Label(head, text=step.docs.replace("docs/", ""), style="Hint.TLabel").pack(
            side="right", padx=(0, 6)
        )

        ttk.Label(self, text=step.summary, style="Summary.TLabel", wraplength=820).pack(
            anchor="w", pady=(4, 2)
        )

        self.warn = ttk.Label(self, text="", style="Warn.TLabel")
        self.warn.pack(anchor="w")

        self.cfg_box = ttk.Frame(self)
        self.cfg_box.pack(fill="x", pady=(2, 4))

        if step.flags:
            flags = ttk.Frame(self)
            flags.pack(fill="x", pady=(0, 4))
            for flag in step.flags:
                if flag.kind == "bool":
                    var = tk.BooleanVar(value=False)
                    self.flag_vars[flag.key] = var
                    ttk.Checkbutton(flags, text=flag.label, variable=var).pack(
                        side="left", padx=(0, 14)
                    )
                elif flag.kind == "choice":
                    ttk.Label(flags, text=flag.label + ":").pack(side="left")
                    var = tk.StringVar(value="")
                    self.flag_vars[flag.key] = var
                    ttk.Combobox(
                        flags,
                        textvariable=var,
                        values=list(flag.choices),
                        width=12,
                        state="readonly",
                    ).pack(side="left", padx=(4, 14))
                else:
                    ttk.Label(flags, text=flag.label + ":").pack(side="left")
                    var = tk.StringVar(value="")
                    self.flag_vars[flag.key] = var
                    ttk.Entry(flags, textvariable=var, width=12).pack(
                        side="left", padx=(4, 14)
                    )

        foot = ttk.Frame(self)
        foot.pack(fill="x", pady=(2, 0))
        ttk.Label(foot, text="Last run:", style="Hint.TLabel").pack(side="left")
        self.when = ttk.Label(foot, text="—", style="When.TLabel")
        self.when.pack(side="left", padx=(6, 0))
        ttk.Button(foot, text="This step", command=self._run_one).pack(side="right")
        ttk.Button(foot, text="From here", command=self._run_from).pack(side="right", padx=(0, 6))

    def flag_values(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, var in self.flag_vars.items():
            out[key] = var.get()
        return out

    def refresh(self, site: dict, slug: str) -> None:
        for child in self.cfg_box.winfo_children():
            child.destroy()
        rows = config_rows(self.step, site)
        if not rows:
            ttk.Label(self.cfg_box, text="No site keys for this step.", style="Hint.TLabel").pack(
                anchor="w"
            )
        else:
            grid = ttk.Frame(self.cfg_box)
            grid.pack(anchor="w")
            for i, (key, value) in enumerate(rows):
                ttk.Label(grid, text=key, style="Key.TLabel").grid(
                    row=i, column=0, sticky="w", padx=(0, 16)
                )
                ttk.Label(grid, text=value, style="Val.TLabel").grid(row=i, column=1, sticky="w")
        if is_applicable(self.step, site):
            self.warn.configure(text="")
        else:
            self.warn.configure(text="Not configured on this site — listed for order, skipped by From here.")
        self.when.configure(text=last_run_label(self.step, site, slug))

    def _run_one(self) -> None:
        self.app.run_steps([self.step])

    def _run_from(self) -> None:
        self.app.run_from(self.step)


class PipelineApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.sites = list_sites()
        if not self.sites:
            raise SystemExit("No files under config/sites/")
        self.site_by_rel = {s.rel: s for s in self.sites}
        self.current_rel = self._initial_site()
        self.proc: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.log_q: queue.Queue[tuple[str, str]] = queue.Queue()
        self.stop_flag = threading.Event()
        self.cards: list[StepCard] = []

        root.title("Autoroad — processing steps")
        root.geometry("980x860")
        root.minsize(820, 640)
        self._style()
        self._build()
        self._reload_site()
        self.root.after(120, self._drain_log)

    def _initial_site(self) -> str:
        env = (os.environ.get("AUTOROAD_SITE") or "").replace("\\", "/").strip()
        if env in self.site_by_rel:
            return env
        state = load_gui_state()
        last = str(state.get("site") or "").replace("\\", "/")
        if last in self.site_by_rel:
            return last
        for rel in (
            "config/sites/fernpass_mega.yaml",
            "config/sites/hahntennjoch.yaml",
        ):
            if rel in self.site_by_rel:
                return rel
        return self.sites[0].rel

    def _style(self) -> None:
        style = ttk.Style(self.root)
        if sys.platform == "win32":
            style.theme_use("vista")
        style.configure("Header.TFrame", padding=10)
        style.configure("Card.TFrame", relief="groove", borderwidth=1)
        style.configure("Num.TLabel", font=("Segoe UI", 12, "bold"), foreground="#445566")
        style.configure("Title.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Summary.TLabel", font=("Segoe UI", 9), foreground="#333333")
        style.configure("Hint.TLabel", font=("Segoe UI", 8), foreground="#666666")
        style.configure("Key.TLabel", font=("Consolas", 8), foreground="#555555")
        style.configure("Val.TLabel", font=("Segoe UI", 9))
        style.configure("When.TLabel", font=("Segoe UI", 9))
        style.configure("Warn.TLabel", font=("Segoe UI", 8), foreground="#8a5a00")
        style.configure("Badge.TLabel", font=("Segoe UI", 8), foreground="#0b5")
        style.configure("Group.TLabel", font=("Segoe UI", 10, "bold"), foreground="#1f3b5b")
        style.configure("Meta.TLabel", font=("Segoe UI", 9))

    def _build(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.pack(fill="x")

        ttk.Label(header, text="Map", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.site_var = tk.StringVar()
        labels = [f"{s.label}  —  {s.rel}" for s in self.sites]
        self.site_combo = ttk.Combobox(
            header,
            textvariable=self.site_var,
            values=labels,
            state="readonly",
            width=58,
        )
        self.site_combo.grid(row=0, column=1, sticky="we", padx=(8, 8))
        self.site_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_site_change())
        ttk.Button(header, text="Open YAML", command=self._open_yaml).grid(row=0, column=2, padx=(0, 4))
        ttk.Button(header, text="Open processed", command=self._open_processed).grid(row=0, column=3)

        self.meta = ttk.Label(header, text="", style="Meta.TLabel")
        self.meta.grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        actions = ttk.Frame(header)
        actions.grid(row=2, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Button(actions, text="Core steps", command=self._run_core).pack(side="left")
        self.stop_btn = ttk.Button(actions, text="Cancel", command=self._stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0))
        ttk.Label(
            actions,
            text="Core = build_level order.  From here = every matching step from the selected card.",
            style="Hint.TLabel",
        ).pack(side="left", padx=(16, 0))
        header.columnconfigure(1, weight=1)

        body = ttk.Panedwindow(self.root, orient="vertical")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        list_wrap = ttk.Frame(body)
        self.canvas = tk.Canvas(list_wrap, highlightthickness=0)
        scroll = ttk.Scrollbar(list_wrap, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind(
            "<Configure>",
            lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas_win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_resize)
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        current_group = ""
        n = 0
        for step in STEPS:
            if step.group != current_group:
                current_group = step.group
                ttk.Label(self.inner, text=current_group, style="Group.TLabel").pack(
                    anchor="w", pady=(12, 4)
                )
            n += 1
            card = StepCard(self.inner, self, step, n)
            card.pack(fill="x", pady=(0, 8))
            self.cards.append(card)

        log_wrap = ttk.Frame(body)
        ttk.Label(log_wrap, text="Output", style="Title.TLabel").pack(anchor="w")
        self.log = tk.Text(log_wrap, height=12, wrap="word", font=("Consolas", 9), state="disabled")
        log_scroll = ttk.Scrollbar(log_wrap, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="left", fill="both", expand=True, pady=(4, 0))
        log_scroll.pack(side="right", fill="y", pady=(4, 0))
        self.log.tag_configure("err", foreground="#a40000")
        self.log.tag_configure("ok", foreground="#0a6b2d")
        self.log.tag_configure("meta", foreground="#555555")

        body.add(list_wrap, weight=3)
        body.add(log_wrap, weight=1)

    def _on_canvas_resize(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        self.canvas.itemconfigure(self.canvas_win, width=event.width)

    def _on_wheel(self, event: tk.Event) -> None:  # type: ignore[type-arg]
        if event.widget is self.log:
            return
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def _site(self):
        return self.site_by_rel[self.current_rel]

    def _on_site_change(self) -> None:
        text = self.site_var.get()
        for info in self.sites:
            if text.endswith(info.rel):
                self.current_rel = info.rel
                break
        save_gui_state({"site": self.current_rel})
        self._reload_site()

    def _reload_site(self) -> None:
        info = self._site()
        self.site_var.set(f"{info.label}  —  {info.rel}")
        size = f"{info.mask_size}²" if info.mask_size else "size unset"
        self.meta.configure(
            text=(
                f"{info.slug}   ·   Level {info.level_name or '—'}   ·   "
                f"{size}   ·   {info.crs or 'CRS unset'}"
            )
        )
        site = info.data
        for card in self.cards:
            card.refresh(site, info.slug)

    def _open_yaml(self) -> None:
        open_path(self._site().path)

    def _open_processed(self) -> None:
        path = ROOT / "data" / "processed" / self._site().slug
        path.mkdir(parents=True, exist_ok=True)
        open_path(path)

    def _append(self, text: str, tag: str = "") -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text, (tag,) if tag else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain_log(self) -> None:
        try:
            while True:
                tag, text = self.log_q.get_nowait()
                self._append(text, tag)
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def _busy(self, on: bool) -> None:
        self.stop_btn.configure(state="normal" if on else "disabled")
        state = "disabled" if on else "readonly"
        self.site_combo.configure(state=state)

    def _run_core(self) -> None:
        self.run_steps([STEPS_BY_ID[s] for s in CORE_STEP_IDS])

    def run_from(self, start: Step) -> None:
        info = self._site()
        picked: list[Step] = []
        seen_start = False
        for step in STEPS:
            if step.id == start.id:
                seen_start = True
            if not seen_start or not in_site_sequence(step):
                continue
            if step.id == start.id or is_applicable(step, info.data):
                picked.append(step)
        self.run_steps(picked)

    def _all_flags(self) -> dict[str, dict[str, Any]]:
        return {card.step.id: card.flag_values() for card in self.cards}

    def run_steps(self, steps: list[Step]) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already running", "Cancel the current run or wait for it to finish.")
            return
        if not steps:
            return
        self.stop_flag.clear()
        flags_for = self._all_flags()
        info = self._site()
        self._busy(True)
        self._append(
            f"\n=== {info.label}: {len(steps)} step(s) ===\n",
            "meta",
        )
        self.worker = threading.Thread(
            target=self._worker,
            args=(list(steps), flags_for, info.rel, info.level_name, info.slug),
            daemon=True,
        )
        self.worker.start()

    def _worker(
        self,
        steps: list[Step],
        flags_for: dict[str, dict[str, Any]],
        site_rel: str,
        level_name: str,
        slug: str,
    ) -> None:
        env = os.environ.copy()
        env["AUTOROAD_SITE"] = site_rel
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        ok_all = True
        for step in steps:
            if self.stop_flag.is_set():
                self.log_q.put(("meta", "Cancelled.\n"))
                ok_all = False
                break
            flags = flags_for.get(step.id) or {}
            cmd = command_for(step, site_rel=site_rel, level_name=level_name, flags=flags)
            self.log_q.put(("meta", f"\n--- {step.title} ---\n{' '.join(cmd)}\n"))
            t0 = time.perf_counter()
            try:
                self.proc = subprocess.Popen(
                    cmd,
                    cwd=str(ROOT),
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    self.log_q.put(("", line))
                code = self.proc.wait()
            except OSError as exc:
                self.log_q.put(("err", f"{exc}\n"))
                code = 1
            finally:
                self.proc = None
            seconds = time.perf_counter() - t0
            ok = code == 0
            record_run(slug, step.id, ok=ok, seconds=seconds)
            if ok:
                self.log_q.put(("ok", f"Done ({seconds:.1f} s)\n"))
            else:
                ok_all = False
                self.log_q.put(("err", f"Exited with code {code} ({seconds:.1f} s)\n"))
                break
        self.log_q.put(("ok" if ok_all else "meta", "\nRun finished.\n"))
        self.root.after(0, self._after_run)

    def _after_run(self) -> None:
        self._busy(False)
        self._reload_site()

    def _stop(self) -> None:
        self.stop_flag.set()
        proc = self.proc
        if proc and proc.poll() is None:
            proc.terminate()
        self.log_q.put(("meta", "Cancel requested.\n"))


def main() -> None:
    root = tk.Tk()
    PipelineApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
