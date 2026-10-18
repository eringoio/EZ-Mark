"""Small optional startup splash for the Windows launcher."""

from __future__ import annotations

import logging


class StartupSplash:
    def __init__(self, language: str = "ja") -> None:
        self._root = None
        self._status_var = None
        self._progress = None
        try:
            import tkinter as tk
            from tkinter import ttk

            messages = startup_messages(language)
            root = tk.Tk()
            root.title("EZ Mark")
            root.resizable(False, False)
            root.geometry("460x150")
            root.protocol("WM_DELETE_WINDOW", lambda: None)

            frame = ttk.Frame(root, padding=18)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text=messages["headline"], wraplength=410).pack(anchor="w")
            status_var = tk.StringVar(value=messages["checking"])
            ttk.Label(frame, textvariable=status_var).pack(anchor="w", pady=(14, 8))
            progress = ttk.Progressbar(frame, mode="indeterminate", length=410)
            progress.pack(fill="x")
            progress.start(12)

            root.update_idletasks()
            self._root = root
            self._status_var = status_var
            self._progress = progress
        except Exception:
            logging.warning("Could not show startup splash", exc_info=True)

    def update(self, message: str) -> None:
        if self._root is None or self._status_var is None:
            return
        try:
            self._status_var.set(message)
            self._root.update_idletasks()
            self._root.update()
        except Exception:
            logging.warning("Could not update startup splash", exc_info=True)

    def close(self) -> None:
        if self._root is None:
            return
        try:
            if self._progress is not None:
                self._progress.stop()
            self._root.destroy()
        except Exception:
            logging.warning("Could not close startup splash", exc_info=True)
        finally:
            self._root = None


def startup_messages(language: str = "ja") -> dict[str, str]:
    if language == "en":
        return {
            "headline": "Starting EZ Mark. This may take a moment, especially on first launch.",
            "checking": "Checking local server...",
            "starting": "Starting server...",
            "loading": "Loading app...",
            "almost": "Almost ready...",
        }
    return {
        "headline": "EZ Markを起動しています。初回起動には少し時間がかかる場合があります。",
        "checking": "ローカルサーバーを確認しています…",
        "starting": "サーバーを起動しています…",
        "loading": "アプリを読み込んでいます…",
        "almost": "もうすぐ完了します…",
    }


def create_startup_splash(language: str = "ja") -> StartupSplash:
    return StartupSplash(language)
