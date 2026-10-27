# -*- coding: utf-8 -*-
"""Point d'entrée : double-cliquer sur Lancer_Bordereaux.bat (ou sur ce fichier)."""
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        import gui
        gui.run()
    except Exception:  # noqa: BLE001 - sous pythonw aucune erreur n'est visible sinon
        text = traceback.format_exc()
        try:
            import core
            folder = core.default_data_dir()
            folder.mkdir(parents=True, exist_ok=True)
            with open(folder / 'erreurs.log', 'a', encoding='utf-8') as f:
                f.write(text + '\n')
        except Exception:  # noqa: BLE001
            pass
        try:
            import tkinter
            from tkinter import messagebox
            root = tkinter.Tk()
            root.withdraw()
            messagebox.showerror('Bordereaux', 'Le programme ne peut pas démarrer :\n\n' + text[-1500:])
        except Exception:  # noqa: BLE001
            pass
        sys.exit(1)


main()
