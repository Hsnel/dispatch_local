# -*- coding: utf-8 -*-
"""Interface de l'application Bordereaux (tkinter, bibliothèque standard uniquement)."""
from __future__ import annotations

import datetime as dt
import os
import time
import tkinter as tk
import tkinter.font as tkfont
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import core

APP_DIR = Path(__file__).resolve().parent
GREEN, RED, GREY, AMBER = '#15803d', '#b91c1c', '#6b7280', '#b45309'
PERSONAL_TEMPLATE = 'Mme / M. : \nGrade : \nPPR : \nCIN : '


def refs(rec) -> str:
    return core.ref(rec['annee'], rec['numero'])


def fr(iso: str) -> str:
    y, m, d = iso.split('-')
    return f'{d}/{m}/{y}'


# ======================================================================== lignes du tableau
class LinesEditor(ttk.Frame):
    """Liste de lignes (désignation, nombre) de longueur libre, avec défilement."""

    def __init__(self, master, height=120):
        super().__init__(master)
        self.rows = []
        head = ttk.Frame(self)
        head.pack(fill='x')
        ttk.Label(head, text='Désignation (une pièce par ligne)').pack(side='left')
        ttk.Label(head, text='Nbr', width=7, anchor='center').pack(side='right', padx=(0, 38))
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        self.canvas = tk.Canvas(body, height=height, highlightthickness=0)
        self.vsb = ttk.Scrollbar(body, orient='vertical', command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.win = self.canvas.create_window((0, 0), window=self.inner, anchor='nw')
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.win, width=e.width))
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side='left', fill='both', expand=True)
        self.vsb.pack(side='right', fill='y')
        self.canvas.bind('<Enter>', lambda e: self.canvas.bind_all('<MouseWheel>', self._wheel))
        self.canvas.bind('<Leave>', lambda e: self.canvas.unbind_all('<MouseWheel>'))
        ttk.Button(self, text='+ Ajouter une ligne', command=lambda: self.add_row(focus=True)).pack(anchor='w', pady=(4, 0))

    def _wheel(self, event):
        self.canvas.yview_scroll(int(-event.delta / 120) or (-1 if event.delta > 0 else 1), 'units')

    def add_row(self, des='', nb='', focus=False):
        fr_ = ttk.Frame(self.inner)
        fr_.pack(fill='x', pady=1)
        d, n = tk.StringVar(value=des), tk.StringVar(value=nb)
        e1 = ttk.Entry(fr_, textvariable=d)
        e1.pack(side='left', fill='x', expand=True, padx=(0, 4))
        e2 = ttk.Entry(fr_, textvariable=n, width=7, justify='center')
        e2.pack(side='left', padx=(0, 4))
        row = {'frame': fr_, 'des': d, 'nb': n, 'e1': e1, 'e2': e2}
        ttk.Button(fr_, text='X', width=3, command=lambda r=row: self.remove_row(r)).pack(side='left')
        e1.bind('<Return>', lambda e: (e2.focus_set(), 'break')[1])
        e2.bind('<Return>', lambda e, r=row: self._enter_in_nb(r))
        self.rows.append(row)
        if focus:
            e1.focus_set()
            self.update_idletasks()
            self.canvas.yview_moveto(1.0)
        return row

    def _enter_in_nb(self, row):
        if row is self.rows[-1]:
            self.add_row(focus=True)
        else:
            self.rows[self.rows.index(row) + 1]['e1'].focus_set()
        return 'break'

    def remove_row(self, row):
        if len(self.rows) == 1:
            row['des'].set('')
            row['nb'].set('')
            return
        self.rows.remove(row)
        row['frame'].destroy()

    def set(self, lines):
        for r in self.rows:
            r['frame'].destroy()
        self.rows = []
        for des, nb in lines:
            self.add_row(des, nb)
        while len(self.rows) < 3:
            self.add_row()
        self.canvas.yview_moveto(0)

    def get(self):
        return [[r['des'].get(), r['nb'].get()] for r in self.rows]


# ======================================================================== onglet « Nouveau »
class NewTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=12)
        self.app = app
        self.edit_id = None
        self.v_date = tk.StringVar()
        self.cur_official = None        # n° officiel du bordereau en cours de modification (conservé tel quel)
        self.v_dest, self.v_titre, self.v_obs = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.v_fonction, self.v_conc = tk.StringVar(), tk.BooleanVar(value=False)
        self.columnconfigure(1, weight=1)
        bold = tkfont.nametofont('TkDefaultFont').copy()
        bold.configure(weight='bold')

        self.banner = ttk.Label(self, text='', foreground=AMBER, font=bold)
        self.banner.grid(row=0, column=0, columnspan=2, sticky='w')

        top = ttk.Frame(self)
        top.grid(row=1, column=0, columnspan=2, sticky='w', pady=(4, 8))
        ttk.Label(top, text='Date :').pack(side='left')
        self.e_date = ttk.Entry(top, textvariable=self.v_date, width=12)
        self.e_date.pack(side='left', padx=(4, 4))
        ttk.Button(top, text="Aujourd'hui", command=self._today).pack(side='left')
        self.hint = ttk.Label(top, text="        N° officiel : à saisir au retour du bordereau signé (onglet Rechercher)",
                              foreground=GREY)
        self.hint.pack(side='left')

        ttk.Label(self, text='Destinataire :').grid(row=2, column=0, sticky='w', pady=3)
        self.c_dest = ttk.Combobox(self, textvariable=self.v_dest)
        self.c_dest.grid(row=2, column=1, sticky='ew', pady=3)
        ttk.Label(self, text='Titre (objet) :').grid(row=3, column=0, sticky='w', pady=3)
        self.c_titre = ttk.Combobox(self, textvariable=self.v_titre)
        self.c_titre.grid(row=3, column=1, sticky='ew', pady=3)

        box = ttk.LabelFrame(self, text=' Tableau du bordereau ', padding=8)
        box.grid(row=4, column=0, columnspan=2, sticky='nsew', pady=(8, 4))
        self.rowconfigure(4, weight=1)
        self.lines = LinesEditor(box)
        self.lines.pack(fill='both', expand=True)

        ttk.Label(self, text='Observation :').grid(row=5, column=0, sticky='w', pady=3)
        ttk.Entry(self, textvariable=self.v_obs).grid(row=5, column=1, sticky='ew', pady=3)

        ttk.Checkbutton(self, text='Ajouter un bloc « Concernant » (personne concernée)', variable=self.v_conc,
                        command=self._toggle_conc).grid(row=6, column=0, columnspan=2, sticky='w', pady=(6, 0))
        self.conc = ttk.Frame(self)
        self.conc.grid(row=7, column=0, columnspan=2, sticky='ew')
        self.conc.columnconfigure(1, weight=1)
        ttk.Label(self.conc, text='Une ligne par information :').grid(row=0, column=0, sticky='nw', padx=(18, 6))
        self.t_conc = tk.Text(self.conc, height=4, width=50, wrap='word')
        self.t_conc.grid(row=0, column=1, sticky='ew', pady=2)
        ttk.Button(self.conc, text='Pré-remplir : nom, grade, PPR, CIN',
                   command=self._prefill).grid(row=0, column=2, sticky='n', padx=6)
        ttk.Label(self.conc, text='En fonction à :').grid(row=1, column=0, sticky='w', padx=(18, 6), pady=2)
        ttk.Entry(self.conc, textvariable=self.v_fonction).grid(row=1, column=1, sticky='ew', pady=2)
        self.conc.grid_remove()

        btns = ttk.Frame(self)
        btns.grid(row=8, column=0, columnspan=2, sticky='ew', pady=(10, 0))
        self.b_main = ttk.Button(btns, text='Enregistrer et ouvrir le PDF', command=lambda: self.save(True))
        self.b_main.pack(side='left')
        self.b_only = ttk.Button(btns, text='Enregistrer seulement', command=lambda: self.save(False))
        self.b_only.pack(side='left', padx=6)
        self._cooldown = False
        self._pdf_busy = False
        ttk.Button(btns, text='Effacer le formulaire', command=self.ask_reset).pack(side='left')
        self.b_cancel = ttk.Button(btns, text='Annuler la modification', command=self.ask_reset)
        self.status = ttk.Label(self, text='', foreground=GREEN)
        self.status.grid(row=9, column=0, columnspan=2, sticky='w', pady=(6, 0))
        self.reset()

    # ---- helpers
    def say(self, text, color=GREEN):
        self.status.configure(text=text, foreground=color)

    def _toggle_conc(self):
        if self.v_conc.get():
            self.conc.grid()
        else:
            self.conc.grid_remove()

    def _prefill(self):
        if not self.t_conc.get('1.0', 'end').strip():
            self.t_conc.insert('1.0', PERSONAL_TEMPLATE)

    def _today(self):
        self.v_date.set(dt.date.today().strftime('%d/%m/%Y'))

    def refresh_choices(self):
        self.c_dest['values'] = self.app.store.recent_values('destinataire')
        self.c_titre['values'] = self.app.store.recent_values('titre')

    # ---- états du formulaire
    def reset(self):
        self.edit_id = None
        self.banner.configure(text='')
        self.b_cancel.pack_forget()
        for v in (self.v_dest, self.v_titre, self.v_obs, self.v_fonction):
            v.set('')
        self.v_dest.set(self.app.store.doc_cfg.get('destinataire_defaut', '').strip())
        self.t_conc.delete('1.0', 'end')
        self.v_conc.set(False)
        self._toggle_conc()
        self.lines.set([])
        self.v_date.set(dt.date.today().strftime('%d/%m/%Y'))
        self.cur_official = None
        self.refresh_choices()
        (self.c_titre if self.v_dest.get() else self.c_dest).focus_set()
        self.mark_clean()

    def load(self, rec, mode):
        self.reset()
        self.v_dest.set(rec['destinataire'])
        self.v_titre.set(rec['titre'])
        self.v_obs.set(rec['observation'])
        self.lines.set(rec['lignes'])
        if rec['concernant'] or rec['en_fonction']:
            self.v_conc.set(True)
            self._toggle_conc()
            self.t_conc.insert('1.0', rec['concernant'])
            self.v_fonction.set(rec['en_fonction'])
        if mode == 'edit':
            self.edit_id = rec['id']
            self.cur_official = rec['officiel']
            self.v_date.set(fr(rec['date']))
            self.banner.configure(text=f"Modification du bordereau {refs(rec)} du {fr(rec['date'])} "
                                       "(l'ancienne version est conservée)")
            self.b_cancel.pack(side='left', padx=6)
        else:
            self.banner.configure(text=f"Nouveau bordereau créé à partir du {refs(rec)} : vérifier la date et le contenu")
        self.say('')
        self.mark_clean()

    def get_data(self):
        return {
            'officiel': self.cur_official, 'date': self.v_date.get(), 'destinataire': self.v_dest.get(),
            'titre': self.v_titre.get(), 'observation': self.v_obs.get(), 'lignes': self.lines.get(),
            'concernant': self.t_conc.get('1.0', 'end') if self.v_conc.get() else '',
            'en_fonction': self.v_fonction.get() if self.v_conc.get() else '',
        }

    def _state(self):
        d = self.get_data()
        return (tuple(map(tuple, d['lignes'])),) + tuple(v for k, v in sorted(d.items()) if k != 'lignes')

    def mark_clean(self):
        self._clean = self._state()

    def is_dirty(self):
        """Vrai si le formulaire contient une saisie non enregistrée (différente de l'état vierge / chargé)."""
        return self._state() != self._clean

    def confirm_discard(self, action):
        if not self.is_dirty():
            return True
        return messagebox.askyesno('Informations non enregistrées',
                                   f"Le formulaire contient des informations non enregistrées.\n\n{action} ?",
                                   default='no', icon='warning', parent=self)

    def ask_reset(self):
        if self.confirm_discard('Effacer le formulaire quand même'):
            self.reset()

    def set_buttons(self):
        """Les boutons d'enregistrement sont grisés pendant 1 s après un enregistrement (double-clic) ;
        « Enregistrer et ouvrir le PDF » l'est aussi tant qu'un PDF est en cours de création."""
        self.b_only.configure(state='disabled' if self._cooldown else 'normal')
        self.b_main.configure(state='disabled' if (self._cooldown or self._pdf_busy) else 'normal')

    def _end_cooldown(self):
        self._cooldown = False
        self.set_buttons()

    def save(self, open_pdf):
        if self._cooldown:
            return
        try:
            rec = self.app.store.save(self.get_data(), edit_id=self.edit_id)
        except core.UserError as e:
            messagebox.showwarning('À corriger', str(e), parent=self)
            return
        except Exception as e:  # noqa: BLE001
            self.app.fatal(e)
            return
        self._cooldown = True
        self.set_buttons()
        self.after(1000, self._end_cooldown)
        was_edit = self.edit_id is not None
        self.reset()
        self.say(f"Bordereau {refs(rec)} {'modifié' if was_edit else 'enregistré'} ({fr(rec['date'])}). "
                 "Son n° officiel sera saisi au retour du bordereau signé.")
        self.app.after_change()
        if open_pdf:
            self.app.open_pdf(rec)


# ======================================================================== onglet « Rechercher »
class SearchTab(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master, padding=12)
        self.app = app
        self._job = None
        self.v_text, self.v_year, self.v_void = tk.StringVar(), tk.StringVar(value='Toutes'), tk.BooleanVar(value=False)
        self.v_nonum = tk.BooleanVar(value=False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=3)
        self.rowconfigure(3, weight=2)

        top = ttk.Frame(self)
        top.grid(row=0, column=0, sticky='ew', pady=(0, 8))
        ttk.Label(top, text='Rechercher :').pack(side='left')
        self.e_text = ttk.Entry(top, textvariable=self.v_text, width=28)
        self.e_text.pack(side='left', padx=6)
        ttk.Label(top, text='Année :').pack(side='left', padx=(12, 0))
        self.c_year = ttk.Combobox(top, textvariable=self.v_year, width=8, state='readonly', values=['Toutes'])
        self.c_year.pack(side='left', padx=6)
        ttk.Checkbutton(top, text='Afficher les annulés', variable=self.v_void, command=self.refresh).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(top, text='Sans n° officiel', variable=self.v_nonum, command=self.refresh).pack(side='left', padx=12)
        self.count = ttk.Label(top, text='', foreground=GREY)
        self.count.pack(side='right')
        self.v_text.trace_add('write', lambda *a: self._debounce())
        self.c_year.bind('<<ComboboxSelected>>', lambda e: self.refresh())

        cols = ('ref', 'numero', 'date', 'dest', 'titre', 'obs')
        frame = ttk.Frame(self)
        frame.grid(row=1, column=0, sticky='nsew')
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=cols, show='headings', selectmode='browse')
        for c, t, w in (('ref', 'Réf.', 100), ('numero', 'N° officiel', 85), ('date', 'Date', 95), ('dest', 'Destinataire', 230),
                        ('titre', 'Titre', 330), ('obs', 'Observation', 200)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor='center' if c in ('ref', 'numero', 'date') else 'w', stretch=c in ('dest', 'titre', 'obs'))
        sb = ttk.Scrollbar(frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sb.grid(row=0, column=1, sticky='ns')
        self.tree.tag_configure('void', foreground='#9ca3af')
        self.tree.bind('<<TreeviewSelect>>', lambda e: self._show())
        self.tree.bind('<Double-1>', lambda e: self._do(self.app.open_pdf))

        btns = ttk.Frame(self)
        btns.grid(row=2, column=0, sticky='ew', pady=8)
        self.b_pdf = None
        for text, cmd in (('Ouvrir le PDF', lambda: self._do(self.app.open_pdf)),
                          ('Ouvrir dans Writer', lambda: self._do(self.app.open_writer)),
                          ('Dupliquer', lambda: self._do(lambda r: self.app.start_form(r, 'dup'))),
                          ('Modifier', lambda: self._do(lambda r: self.app.start_form(r, 'edit'))),
                          ('Saisir le n° officiel', lambda: self._do(self.app.set_official)),
                          ('Annuler ce bordereau', lambda: self._do(self.app.void))):
            b = ttk.Button(btns, text=text, command=cmd)
            b.pack(side='left', padx=(0, 6))
            if self.b_pdf is None:
                self.b_pdf = b
        ttk.Button(btns, text='Exporter la liste…', command=self.export).pack(side='right')

        dframe = ttk.Frame(self)
        dframe.grid(row=3, column=0, sticky='nsew')
        dframe.columnconfigure(0, weight=1)
        dframe.rowconfigure(0, weight=1)
        self.details = tk.Text(dframe, height=8, wrap='word', state='disabled', background='#f9fafb', relief='flat')
        dsb = ttk.Scrollbar(dframe, orient='vertical', command=self.details.yview)
        self.details.configure(yscrollcommand=dsb.set)
        self.details.grid(row=0, column=0, sticky='nsew')
        dsb.grid(row=0, column=1, sticky='ns')

    def _debounce(self):
        if self._job:
            self.after_cancel(self._job)
        self._job = self.after(200, self.refresh)

    def refresh(self):
        self._job = None
        years = ['Toutes'] + [str(y) for y in self.app.store.years()]
        self.c_year['values'] = years
        if self.v_year.get() not in years:
            self.v_year.set('Toutes')
        year = None if self.v_year.get() == 'Toutes' else int(self.v_year.get())
        recs = self.app.store.search(self.v_text.get(), year, self.v_void.get(), sans_numero=self.v_nonum.get())
        self.tree.delete(*self.tree.get_children())
        for r in recs:
            obs = r['observation'] + ('   [ANNULÉ]' if r['annule'] else '')
            self.tree.insert('', 'end', iid=str(r['id']), tags=('void',) if r['annule'] else (),
                             values=(refs(r), r['officiel'] or '—', fr(r['date']), r['destinataire'], r['titre'], obs))
        self.count.configure(text=f'{len(recs)} bordereau(x)')
        self._show()

    def current(self):
        sel = self.tree.selection()
        return self.app.store.get(int(sel[0])) if sel else None

    def export(self):
        year = None if self.v_year.get() == 'Toutes' else int(self.v_year.get())
        args = dict(text=self.v_text.get(), annee=year, include_void=self.v_void.get(), sans_numero=self.v_nonum.get())
        n = len(self.app.store.search(limit=100000, **args))
        if n == 0:
            messagebox.showinfo('Exporter la liste', "Aucun bordereau dans la liste affichée.", parent=self)
            return
        name = f'Liste_Bordereaux_{dt.date.today().isoformat()}.ods'
        dest = filedialog.asksaveasfilename(parent=self, title='Enregistrer la liste', initialfile=name,
                                            defaultextension='.ods',
                                            filetypes=[('LibreOffice Calc (.ods)', '*.ods'), ('CSV (Excel)', '*.csv')])
        if not dest:
            return
        try:
            n = self.app.store.export_list(dest, **args)
        except Exception as e:  # noqa: BLE001
            self.app.fatal(e)
            return
        messagebox.showinfo('Liste enregistrée', f"{n} bordereau(x) enregistré(s) dans :\n{dest}\n\n"
                            "La liste reprend exactement ce qui est affiché (recherche, année, filtres), du plus ancien "
                            "au plus récent. Elle contient les destinataires et les titres, pas le tableau ni le bloc "
                            "« Concernant ». La ranger comme les autres documents de l'organisation.", parent=self)

    def _do(self, action):
        rec = self.current()
        if rec is None:
            messagebox.showinfo('Bordereaux', "Sélectionner d'abord un bordereau dans la liste.", parent=self)
            return
        action(rec)

    def _show(self):
        rec = self.current()
        lines = []
        if rec:
            lines = [f"Réf. {refs(rec)}   —   N° officiel : {rec['officiel'] or 'pas encore saisi'}   —   du {fr(rec['date'])}"
                     + ('    [ANNULÉ]' if rec['annule'] else ''),
                     f"Destinataire : {rec['destinataire']}", f"Titre : {rec['titre']}", '']
            lines += [f"   •  {des}" + (f"     ({nb})" if nb else '') for des, nb in rec['lignes']]
            if rec['observation']:
                lines += ['', f"Observation : {rec['observation']}"]
            if rec['concernant']:
                lines += ['', 'Concernant :'] + ['   ' + x for x in rec['concernant'].split('\n')]
            if rec['en_fonction']:
                lines.append(f"En fonction à : {rec['en_fonction']}")
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', '\n'.join(lines))
        self.details.configure(state='disabled')


# ======================================================================== fenêtre principale
class App(tk.Tk):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.title("Bordereaux d'envoi — version pilote")
        height = max(600, min(780, self.winfo_screenheight() - 90))   # tient sur un écran 1366 x 768
        self.geometry(f'1000x{height}+20+10')
        self.minsize(880, 600)
        base = tkfont.nametofont('TkDefaultFont')
        base.configure(size=10)
        tkfont.nametofont('TkTextFont').configure(size=10)
        self.report_callback_exception = lambda et, ev, tb: self.fatal(ev, tb)

        menu = tk.Menu(self)
        m_file = tk.Menu(menu, tearoff=False)
        m_file.add_command(label='Ouvrir le dossier des archives', command=self.open_archives)
        m_file.add_command(label='Faire une copie de sauvegarde (zip)…', command=self.backup)
        m_file.add_command(label="Reconstruire l'index depuis les fichiers…", command=self.rebuild)
        m_file.add_separator()
        m_file.add_command(label='Quitter', command=self.on_close)
        menu.add_cascade(label='Fichier', menu=m_file)
        m_help = tk.Menu(menu, tearoff=False)
        m_help.add_command(label='Créer un rapport de diagnostic (zip)…', command=self.diagnostic)
        m_help.add_command(label='Ouvrir le dossier des journaux', command=self.open_logs)
        m_help.add_separator()
        m_help.add_command(label='À propos', command=self.about)
        menu.add_cascade(label='Aide', menu=m_help)
        self.configure(menu=menu)

        self._job, self._job_rec, self._poll_id, self._last_open = None, None, None, ('', 0.0)
        self.statusbar = ttk.Frame(self, padding=(10, 3))
        self.statusbar.pack(side='bottom', fill='x')
        self.v_status = tk.StringVar(value='')
        ttk.Label(self.statusbar, textvariable=self.v_status, foreground=GREY).pack(side='left')
        self.b_stop = ttk.Button(self.statusbar, text='Annuler le PDF', command=self.cancel_pdf)

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill='both', expand=True)
        self.tab_new = NewTab(self.nb, self)
        self.tab_search = SearchTab(self.nb, self)
        self.nb.add(self.tab_new, text='   Nouveau bordereau   ')
        self.nb.add(self.tab_search, text='   Rechercher   ')
        self.nb.bind('<<NotebookTabChanged>>', lambda e: self.tab_search.refresh() if self.nb.index('current') == 1 else None)
        self.protocol('WM_DELETE_WINDOW', self.on_close)

    # ---- actions communes
    def after_change(self):
        self.tab_new.refresh_choices()
        self.tab_search.refresh()

    def start_form(self, rec, mode):
        if not self.tab_new.confirm_discard('Les remplacer par ce bordereau'):
            self.nb.select(self.tab_new)
            return
        self.tab_new.load(rec, mode)
        self.nb.select(self.tab_new)

    def busy(self, on):
        self.configure(cursor='watch' if on else '')
        self.update_idletasks()

    # ---- PDF en tâche de fond : la fenêtre reste utilisable, un seul PDF à la fois
    def set_pdf_busy(self, on, text=''):
        self.tab_new._pdf_busy = on
        self.tab_new.set_buttons()
        self.tab_search.b_pdf.configure(state='disabled' if on else 'normal')
        self.v_status.set(text)
        if on:
            self.b_stop.pack(side='right')
        else:
            self.b_stop.pack_forget()

    def open_pdf(self, rec):
        if self._job is not None:               # double-clic, clics répétés : on ignore, un PDF est déjà en route
            return
        try:
            job = self.store.start_pdf(rec['id'])
        except core.UserError as e:
            messagebox.showwarning('PDF', str(e), parent=self)
            return
        except core.PdfError as e:
            self._pdf_failed(e, rec)
            return
        self._job, self._job_rec = job, rec
        self.set_pdf_busy(True, f"Création du PDF du bordereau {refs(rec)} en cours…")
        self._poll_pdf()

    def _poll_pdf(self):
        self._poll_id = None
        job, rec = self._job, self._job_rec
        if job is None:
            return
        try:
            pdf = job.poll()
        except core.PdfError as e:
            self._end_job()
            self._pdf_failed(e, rec)
            return
        except Exception as e:  # noqa: BLE001
            self._end_job()
            self.fatal(e)
            return
        if pdf is None:
            secs = int(time.monotonic() - job.t0)
            extra = ' (le premier PDF est plus long)' if secs >= 8 else ''
            self.v_status.set(f"Création du PDF du bordereau {refs(rec)} en cours… {secs} s{extra}")
            self._poll_id = self.after(150, self._poll_pdf)
            return
        self._end_job()
        self._open(pdf, "Aucune application n'est associée aux fichiers PDF sur ce poste.")

    def _end_job(self):
        if self._poll_id is not None:
            self.after_cancel(self._poll_id)
            self._poll_id = None
        self._job = self._job_rec = None
        self.set_pdf_busy(False)

    def cancel_pdf(self):
        if self._job is not None:
            self._job.cancel()
            self._end_job()
            self.v_status.set('Création du PDF annulée.')

    def _pdf_failed(self, err, rec):
        if messagebox.askyesno('PDF impossible', f'{err}\n\nOuvrir le document dans Writer à la place ?', parent=self):
            self.open_writer(rec)

    def open_writer(self, rec):
        self._open(self.store.doc_path(rec), "Impossible d'ouvrir le document (LibreOffice Writer est-il installé ?).")

    def _open(self, path, err):
        now = time.monotonic()
        if self._last_open[0] == str(path) and now - self._last_open[1] < 1.5:   # clics répétés : une seule ouverture
            return
        self._last_open = (str(path), now)
        try:
            core.open_with_system(path)
        except OSError as e:
            core.LOG.error('ouverture impossible (%s) : %s', Path(path).suffix or 'dossier', e.__class__.__name__)
            messagebox.showerror('Ouverture impossible', f'{err}\n\nFichier : {path}', parent=self)

    def void(self, rec):
        if rec['annule']:
            messagebox.showinfo('Bordereaux', 'Ce bordereau est déjà annulé.', parent=self)
            return
        if messagebox.askyesno('Annuler le bordereau',
                               f"Annuler le bordereau {refs(rec)} ?\n\nIl reste visible (grisé) et son n° officiel, s'il en a un, "
                               "ne pourra pas être réutilisé. Cette action ne supprime aucun fichier.", parent=self):
            self.store.void(rec['id'])
            self.after_change()

    def set_official(self, rec):
        raw = simpledialog.askstring('N° officiel', f"N° attribué par le bureau d'ordre au bordereau {refs(rec)} "
                                     "(exemples : 116, 116/2026, 116 bis ; laisser vide pour retirer le numéro) :",
                                     initialvalue='' if rec['officiel'] is None else str(rec['officiel']), parent=self)
        if raw is None:
            return
        try:
            self.store.set_official(rec['id'], raw)
        except core.UserError as e:
            messagebox.showwarning('N° officiel', str(e), parent=self)
            return
        except Exception as e:  # noqa: BLE001
            self.fatal(e)
            return
        self.after_change()

    def open_archives(self):
        self._open(self.store.dir / 'archives', "Impossible d'ouvrir le dossier des archives.")

    def backup(self):
        name = f'Sauvegarde_Bordereaux_{dt.date.today().isoformat()}.zip'
        dest = filedialog.asksaveasfilename(parent=self, title='Enregistrer la copie de sauvegarde', initialfile=name,
                                            defaultextension='.zip', filetypes=[('Archive zip', '*.zip')])
        if not dest:
            return
        self.busy(True)
        try:
            out = self.store.backup_zip(dest)
        finally:
            self.busy(False)
        messagebox.showinfo('Sauvegarde terminée', f'Copie enregistrée :\n{out}\n\nLa ranger sur un support sûr '
                            '(clé chiffrée, dossier protégé). Elle contient des données personnelles.', parent=self)

    def rebuild(self):
        if not messagebox.askyesno('Reconstruire l\'index',
                                   "Relire les documents des archives et ajouter à l'index ceux qui y manquent ?\n"
                                   "Rien n'est supprimé ni modifié.", parent=self):
            return
        self.busy(True)
        try:
            info = self.store.rebuild_index()
        finally:
            self.busy(False)
        txt = f"{info['ajoutes']} ajouté(s), {info['deja_presents']} déjà présent(s)."
        if info['erreurs']:
            txt += '\n\nFichiers ignorés :\n' + '\n'.join(info['erreurs'][:10])
        messagebox.showinfo('Index', txt, parent=self)
        self.after_change()

    def about(self):
        soffice = core.find_soffice(self.store.cfg['general'].get('libreoffice', '')) or 'introuvable'
        messagebox.showinfo('À propos',
                            f"Bordereaux — version {core.VERSION}\n\nDonnées : {self.store.dir}\nLibreOffice : {soffice}\n\n"
                            "Application locale : aucune connexion réseau, aucun compte.", parent=self)

    def open_logs(self):
        self._open(self.store.dir / 'journaux', "Impossible d'ouvrir le dossier des journaux.")

    def diagnostic(self):
        name = f'Diagnostic_Bordereaux_{dt.date.today().isoformat()}.zip'
        dest = filedialog.asksaveasfilename(parent=self, title='Enregistrer le rapport de diagnostic', initialfile=name,
                                            defaultextension='.zip', filetypes=[('Archive zip', '*.zip')])
        if not dest:
            return
        try:
            out = self.store.diagnostic_zip(dest)
        except Exception as e:  # noqa: BLE001
            self.fatal(e)
            return
        messagebox.showinfo('Rapport créé',
                            f"Rapport enregistré :\n{out}\n\nIl contient le journal des événements et une fiche technique "
                            "(version, système, nombre de bordereaux). Il ne contient aucun bordereau, nom, titre, "
                            "destinataire ni terme de recherche. Il indique le nom du poste et de la session Windows.",
                            parent=self)

    def fatal(self, exc, tb=None):
        core.log_exception('interface', exc, tb)
        text = ''.join(traceback.format_exception(type(exc), exc, tb or exc.__traceback__))
        try:
            with open(self.store.dir / 'erreurs.log', 'a', encoding='utf-8') as f:
                f.write(f'--- {dt.datetime.now().isoformat(timespec="seconds")}\n{text}\n')
        except OSError:
            pass
        messagebox.showerror('Erreur', "Une erreur inattendue s'est produite. Rien n'a été perdu : "
                             "les bordereaux déjà enregistrés sont intacts.\n\n"
                             f"{type(exc).__name__}: {exc}\n\nHeure : {dt.datetime.now():%d/%m/%Y %H:%M:%S}   Session : {core.SESSION}\n"
                             "Prévenir le technicien (Aide > Créer un rapport de diagnostic).", parent=self)

    def on_close(self):
        if not self.tab_new.confirm_discard('Quitter quand même'):
            self.nb.select(self.tab_new)
            return
        try:
            if self._poll_id is not None:
                self.after_cancel(self._poll_id)
            self.store.close()      # arrête aussi un PDF en cours
        finally:
            self.destroy()


def run():
    try:
        store = core.open_store(APP_DIR, os.environ.get('BORDEREAUX_DATA') or None)
    except core.AlreadyRunning:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo('Bordereaux', "L'application est déjà ouverte sur ce poste "
                            "(la chercher dans la barre des tâches).", parent=root)
        root.destroy()
        return
    App(store).mainloop()
