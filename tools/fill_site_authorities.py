"""Fill ``authorities`` on a site YAML from GISCO NUTS boundaries.

The site CRS + bbox decide which catalog keys cover the crop. Complete
coverage is written into the YAML. A leftover outside the catalog is reported
and can become a new catalog entry.

```powershell
cd C:\\temp\\beamng_autoroad; python tools\\fill_site_authorities.py
```

```powershell
cd C:\\temp\\beamng_autoroad; python tools\\fill_site_authorities.py --write
```

```powershell
cd C:\\temp\\beamng_autoroad; python tools\\fill_site_authorities.py --add-key bayern --nuts DE21 --crs EPSG:25832
```
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from authorities import (  # noqa: E402
    append_catalog_authority,
    apply_site_authorities,
    cover_site_authorities,
    ensure_nuts_boundaries,
    next_reserved_offset,
)
from site_coords import resolve_site_path  # noqa: E402


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def _dialog_new_authorities(cov) -> None:
    print(cov.summary())
    if not cov.foreign:
        print("Keine NUTS-Fläche für den unbekannten Rest. Katalog von Hand ergänzen.")
        return
    print("Unbekannte NUTS-Gebiete:")
    for i, row in enumerate(cov.foreign, 1):
        print(
            f"  {i}. {row['nuts_id']}  {row['name']}  "
            f"({100.0 * float(row['frac']):.1f} % des Ausschnitts)"
        )
    if not sys.stdin.isatty():
        print("Kein Terminal — neue Behörde mit --add-key anlegen.")
        return
    ans = _prompt("Neue Behörde anlegen? (j/n)", "j")
    if ans.lower() not in {"j", "ja", "y", "yes"}:
        return
    first = cov.foreign[0]
    nuts = _prompt("NUTS-ID", str(first["nuts_id"]))
    default_key = str(first["name"] or nuts).split("/")[0].split()[0].lower()
    default_key = "".join(ch for ch in default_key if ch.isalnum()) or "neu"
    key = _prompt("Katalog-Schlüssel", default_key)
    crs = _prompt("CRS", "EPSG:25832")
    offset_s = _prompt("road_id_offset", str(next_reserved_offset()))
    append_catalog_authority(
        key,
        nuts_id=nuts,
        crs=crs,
        road_id_offset=int(offset_s),
        name=str(first.get("name") or ""),
    )
    print(f"Katalog: {key} ({nuts}) eingetragen.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", help="Site YAML (default: AUTOROAD_SITE / config/site.yaml)")
    ap.add_argument(
        "--write",
        action="store_true",
        help="Write detected catalog keys into the site YAML",
    )
    ap.add_argument("--add-key", help="New catalog key (with --nuts and --crs)")
    ap.add_argument("--nuts", help="NUTS 2024 level-2 id, e.g. ITH1")
    ap.add_argument("--crs", help="Working CRS of the new authority")
    ap.add_argument("--offset", type=int, help="road_id_offset (default: next 10e6 band)")
    args = ap.parse_args()

    if args.add_key:
        if not args.nuts or not args.crs:
            raise SystemExit("--add-key braucht --nuts und --crs")
        ensure_nuts_boundaries()
        append_catalog_authority(
            args.add_key,
            nuts_id=args.nuts,
            crs=args.crs,
            road_id_offset=args.offset,
        )
        print(f"Katalog: {args.add_key} ({args.nuts}) eingetragen.")

    path = resolve_site_path(args.site)
    if not path.is_file():
        raise SystemExit(f"Site fehlt: {path}")
    print(f"Site {path.relative_to(ROOT)}")
    ensure_nuts_boundaries()
    if args.write:
        cov = apply_site_authorities(path, write=True)
        if cov.known:
            print(f"authorities geschrieben: {', '.join(cov.known)}")
    else:
        import yaml

        cov = cover_site_authorities(
            yaml.safe_load(path.read_text(encoding="utf-8")) or {},
            use_nuts=True,
        )
    print(cov.summary())
    if not cov.complete:
        _dialog_new_authorities(cov)
        if args.write and cov.foreign:
            again = apply_site_authorities(path, write=True)
            print(again.summary())
            if again.known:
                print(f"authorities geschrieben: {', '.join(again.known)}")


def show_fill_dialog(parent, path: Path) -> bool:
    """Tk dialog: detect, write known keys, offer to add missing NUTS authorities."""
    import tkinter as tk
    from tkinter import messagebox, simpledialog, ttk

    import yaml

    path = Path(path)
    parent.config(cursor="watch")
    parent.update_idletasks()
    try:
        ensure_nuts_boundaries()
        site = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cov = cover_site_authorities(site, use_nuts=True)
    except Exception as ex:
        parent.config(cursor="")
        messagebox.showerror("Authorities", str(ex), parent=parent)
        return False
    parent.config(cursor="")

    changed = False
    if cov.known:
        if messagebox.askyesno(
            "Authorities",
            cov.summary() + "\n\nBekannte Behörden in die Site-YAML schreiben?",
            parent=parent,
        ):
            apply_site_authorities(path, write=True)
            changed = True
    else:
        messagebox.showwarning("Authorities", cov.summary(), parent=parent)

    if cov.complete:
        return changed

    win = tk.Toplevel(parent)
    win.title("Neue Behörde")
    win.transient(parent)
    ttk.Label(
        win,
        text=(
            "Der Ausschnitt liegt nicht vollständig in den bekannten Behörden.\n"
            "Unten die NUTS-Flächen im unbekannten Rest. Eine davon als "
            "Katalogeintrag anlegen?"
        ),
        wraplength=520,
        justify="left",
    ).pack(anchor="w", padx=12, pady=(12, 8))
    tree = ttk.Treeview(win, columns=("nuts", "name", "pct"), show="headings", height=6)
    tree.heading("nuts", text="NUTS")
    tree.heading("name", text="Name")
    tree.heading("pct", text="%")
    tree.column("nuts", width=80)
    tree.column("name", width=280)
    tree.column("pct", width=60)
    for row in cov.foreign:
        tree.insert(
            "",
            "end",
            values=(
                row["nuts_id"],
                row["name"],
                f"{100.0 * float(row['frac']):.1f}",
            ),
        )
    tree.pack(fill="both", expand=True, padx=12, pady=(0, 8))

    def _add() -> None:
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Authorities", "Zuerst eine NUTS-Zeile wählen.", parent=win)
            return
        nuts_id, name, _pct = tree.item(sel[0], "values")
        default_key = "".join(ch for ch in str(name).split()[0].lower() if ch.isalnum())
        key = simpledialog.askstring(
            "Katalog-Schlüssel",
            f"Schlüssel für {nuts_id} ({name})",
            initialvalue=default_key or str(nuts_id).lower(),
            parent=win,
        )
        if not key:
            return
        crs = simpledialog.askstring(
            "CRS",
            "Koordinatenreferenz der neuen Behörde",
            initialvalue="EPSG:25832",
            parent=win,
        )
        if not crs:
            return
        try:
            append_catalog_authority(
                key.strip(),
                nuts_id=str(nuts_id),
                crs=crs.strip(),
                name=str(name),
            )
            apply_site_authorities(path, write=True)
        except Exception as ex:
            messagebox.showerror("Authorities", str(ex), parent=win)
            return
        nonlocal changed
        changed = True
        win.destroy()

    btns = ttk.Frame(win)
    btns.pack(fill="x", padx=12, pady=(0, 12))
    ttk.Button(btns, text="Als Behörde anlegen", command=_add).pack(side="left")
    ttk.Button(btns, text="Schließen", command=win.destroy).pack(side="right")
    win.grab_set()
    parent.wait_window(win)
    return changed


if __name__ == "__main__":
    main()
