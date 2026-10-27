# -*- coding: utf-8 -*-
"""Logique de l'application Bordereaux : stockage, numérotation, recherche, sauvegarde, PDF.

Bibliothèque standard uniquement. AUCUN accès réseau : le seul programme externe appelé
est LibreOffice (installé sur le poste) pour produire un PDF, et l'application associée
au fichier pour l'ouvrir.
"""
from __future__ import annotations

import configparser
import csv
import datetime as dt
import errno
import json
import logging
import logging.handlers
import os
import platform
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import unicodedata
import zipfile
from pathlib import Path

import odt_builder

APP_NAME = 'Bordereaux'
VERSION = '0.2 (pilote)'
SESSION = secrets.token_hex(2)               # court identifiant de lancement, pour suivre un incident dans le journal
LOG = logging.getLogger('bordereaux')
LOG.propagate = False
LOG.addHandler(logging.NullHandler())
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

DEFAULTS = {
    'general': {
        'dossier_donnees': '',   # vide = dossier privé de l'utilisateur Windows (AppData\Local\BordereauxApp)
        'libreoffice': '',       # vide = détection automatique de soffice.exe
    },
    'document': {
        'reference': 'N° {numero} S.P.A.G/BRH',
        'signataire': 'Le Directeur',
        'logo': '',              # chemin d'une image PNG ou JPEG (logo + titre) pour l'en-tête
        'pied_ligne1': '',       # adresse
        'pied_ligne2': '',       # téléphone, e-mail
        'colonne_nombre': 'Nbr', 'logo_hauteur_max': '6', 'destinataire_defaut': '', 'pied_ligne3': '', 'lignes_vides_avant_date': '0',
    },
}


class PdfError(Exception):
    pass


class UserError(ValueError):
    """Erreur de saisie : message en français pour l'utilisateur, code court pour le journal (jamais de données)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class AlreadyRunning(Exception):
    pass


# ------------------------------------------------------------------ journal
class _SessionFilter(logging.Filter):
    def filter(self, record):
        record.session = SESSION
        return True


def setup_logging(data_dir) -> Path:
    """Journal tournant (5 fichiers de 500 Ko au plus) dans <données>\\journaux.

    Règle : le journal ne contient JAMAIS de nom, titre, destinataire, ligne de tableau ni terme de recherche ;
    seulement des événements, des numéros, des identifiants et des codes d'erreur.
    """
    folder = Path(data_dir) / 'journaux'
    folder.mkdir(parents=True, exist_ok=True)
    for h in list(LOG.handlers):
        LOG.removeHandler(h)
        h.close()
    LOG.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(folder / 'journal.log', maxBytes=500_000, backupCount=4,
                                                   encoding='utf-8')
    handler.addFilter(_SessionFilter())
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)-7s [%(session)s] %(message)s'))
    LOG.addHandler(handler)
    return folder


def log_exception(where, exc, tb=None) -> None:
    """Journalise une erreur inattendue : type + lignes de code seulement, JAMAIS le message (il pourrait citer une saisie)."""
    frames = traceback.extract_tb(tb or exc.__traceback__)
    path = ' > '.join(f'{Path(f.filename).name}:{f.lineno} {f.name}' for f in frames[-6:])
    LOG.error('erreur inattendue (%s) : %s | %s', where, type(exc).__name__, path)


class SingleInstance:
    """Une seule fenêtre de l'application par poste (verrou de fichier libéré par Windows à la fermeture)."""

    def __init__(self, folder):
        self.path = Path(folder) / 'app.lock'
        self.fh = None

    def acquire(self) -> bool:
        try:
            self.fh = open(self.path, 'a+b')
            if self.fh.tell() == 0 and self.path.stat().st_size == 0:
                self.fh.write(b'0')
                self.fh.flush()
            self.fh.seek(0)
            if sys.platform.startswith('win'):
                import msvcrt
                msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            busy = (errno.EACCES, errno.EAGAIN, getattr(errno, 'EDEADLK', 36), 36)
            if self.fh is not None and e.errno in busy:
                self.fh.close()
                self.fh = None
                return False
            LOG.warning("verrou d'instance indisponible (%s) : poursuite sans verrou", e)
            return True

    def release(self):
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
            self.fh = None


def kill_tree(proc) -> None:
    """Arrête un processus et ses enfants (LibreOffice lance soffice.bin à part)."""
    try:
        if sys.platform.startswith('win'):
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=15,
                           creationflags=NO_WINDOW)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001 - on tente aussi l'arrêt simple ci-dessous
        pass
    try:
        proc.kill()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------ configuration
def load_config(app_dir) -> configparser.ConfigParser:
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_dict(DEFAULTS)
    p = Path(app_dir) / 'config.ini'
    if p.is_file():
        raw = p.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:       # fichier enregistré en ANSI par un ancien Bloc-notes
            text = raw.decode('cp1252', 'replace')
        cp.read_string(text)
    return cp


def default_data_dir() -> Path:
    if sys.platform.startswith('win'):
        base = os.environ.get('LOCALAPPDATA') or str(Path.home() / 'AppData' / 'Local')
        return Path(base) / 'BordereauxApp'
    return Path.home() / '.local' / 'share' / 'BordereauxApp'


def find_soffice(configured: str = ''):
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which('soffice') or shutil.which('soffice.exe')
    if found:
        return found
    for base in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
        if base:
            cand = Path(base) / 'LibreOffice' / 'program' / 'soffice.exe'
            if cand.is_file():
                return str(cand)
    for cand in ('/usr/lib/libreoffice/program/soffice', '/opt/libreoffice/program/soffice',
                 '/Applications/LibreOffice.app/Contents/MacOS/soffice'):
        if Path(cand).is_file():
            return cand
    return None


def open_with_system(path) -> None:
    """Ouvre un fichier ou un dossier avec l'application associée du poste."""
    path = str(path)
    if sys.platform.startswith('win'):
        os.startfile(path)  # noqa: S606 - chemins internes uniquement
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', path])
    else:
        subprocess.Popen(['xdg-open', path])


# ------------------------------------------------------------------ validation
def norm(s) -> str:
    s = unicodedata.normalize('NFD', str(s).replace('’', "'").lower())
    s = ''.join(c for c in s if unicodedata.category(c) != 'Mn')
    return ' '.join(s.split())


def parse_date(s) -> dt.date:
    s = str(s).strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%d-%m-%Y', '%d.%m.%Y'):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        if 2000 <= d.year <= 2100:
            return d
        break
    raise UserError('date_invalide', "Date invalide : utiliser le format jj/mm/aaaa (exemple : 22/10/2026).")


def _one_line(s, label, maxlen, required=False):
    s = ' '.join(odt_builder.clean(s).split())
    if required and not s:
        raise UserError('champ_obligatoire', f"Le champ « {label} » est obligatoire.")
    if len(s) > maxlen:
        raise UserError('champ_trop_long', f"Le champ « {label} » est trop long ({maxlen} caractères maximum).")
    return s


def ref(annee, numero) -> str:
    """Référence interne d'un bordereau (jamais imprimée) : 2026-0017."""
    return f'{int(annee)}-{int(numero):04d}'


def _int_field(value, code, label):
    s = '' if value is None else str(value).strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        raise UserError(code, f"{label} : un nombre entier est attendu (exemple : 116).") from None
    if not 1 <= n <= 999999:
        raise UserError(code + '_plage', f"{label} : le nombre doit être compris entre 1 et 999999.")
    return n


_OFFICIEL_RE = re.compile(r"[0-9A-Za-zÀ-ÿ/\-._ ]{1,30}")


def _official_field(value):
    """N° du bureau d'ordre : texte court (chiffres, lettres, / - . _ espace), ex. 116, 116/2026, 116 bis."""
    s = '' if value is None else ' '.join(str(value).split())
    if not s:
        return None
    if not _OFFICIEL_RE.fullmatch(s):
        raise UserError('officiel_invalide', "N° officiel : 30 caractères maximum, avec chiffres, lettres et / - . _ "
                        "seulement (exemples : 116, 116/2026, 116 bis).")
    return s


def validate(data: dict, need_seq=False) -> dict:
    """numero = référence interne (attribuée par l'application) ; officiel = n° du bureau d'ordre (facultatif)."""
    numero = _int_field(data.get('numero'), 'numero_invalide', 'Référence interne')
    if need_seq and numero is None:
        raise UserError('numero_invalide', 'Référence interne absente.')
    officiel = _official_field(data.get('officiel'))
    day = parse_date(data.get('date', ''))
    lignes = []
    for i, pair in enumerate(data.get('lignes') or [], start=1):
        des = _one_line(pair[0], 'Désignation', 500)
        nb = _one_line(pair[1] if len(pair) > 1 else '', 'Nbr', 20)
        if not des and not nb:
            continue
        if not des:
            raise UserError('ligne_sans_designation', f"Ligne {i} : la désignation est vide (le nombre est rempli).")
        lignes.append([des, nb])
    if len(lignes) > 200:
        raise UserError('trop_de_lignes', "Trop de lignes (200 maximum).")
    conc = data.get('concernant', '')
    if isinstance(conc, (list, tuple)):
        conc = '\n'.join(map(str, conc))
    conc_lines = [_one_line(x, 'Concernant', 200) for x in odt_builder.clean(conc).replace('\r', '').split('\n')]
    conc_lines = [x for x in conc_lines if x]
    if len(conc_lines) > 30:
        raise UserError('trop_de_lignes_concernant', "Trop de lignes dans « Concernant » (30 maximum).")
    return {
        'numero': numero,
        'officiel': officiel,
        'annee': day.year,
        'date': day.isoformat(),
        'destinataire': _one_line(data.get('destinataire', ''), 'Destinataire', 300, required=True),
        'titre': _one_line(data.get('titre', ''), 'Titre', 300, required=True),
        'observation': _one_line(data.get('observation', ''), 'Observation', 300),
        'lignes': lignes,
        'concernant': '\n'.join(conc_lines),
        'en_fonction': _one_line(data.get('en_fonction', ''), 'En fonction à', 200),
    }


def search_text(d: dict) -> str:
    parts = [ref(d['annee'], d['numero']), str(d['numero']), str(d['officiel'] or ''), d['date'], odt_builder.fr_date(d['date']), d['destinataire'], d['titre'],
             d['observation'], d['concernant'], d['en_fonction']]
    parts += [f'{des} {nb}' for des, nb in d['lignes']]
    return norm(' '.join(parts))


def _like_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


# ------------------------------------------------------------------ stockage
SCHEMA = """
CREATE TABLE IF NOT EXISTS bordereaux (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  annee INTEGER NOT NULL,
  numero INTEGER NOT NULL,
  officiel TEXT,
  date TEXT NOT NULL,
  destinataire TEXT NOT NULL,
  titre TEXT NOT NULL,
  observation TEXT NOT NULL DEFAULT '',
  lignes TEXT NOT NULL DEFAULT '[]',
  concernant TEXT NOT NULL DEFAULT '',
  en_fonction TEXT NOT NULL DEFAULT '',
  fichier TEXT NOT NULL,
  annule INTEGER NOT NULL DEFAULT 0,
  recherche TEXT NOT NULL DEFAULT '',
  cree_le TEXT NOT NULL,
  modifie_le TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_annee_numero ON bordereaux(annee, numero);
"""
SCHEMA_OFFICIEL = "CREATE UNIQUE INDEX IF NOT EXISTS ux_annee_officiel ON bordereaux(annee, officiel COLLATE NOCASE) WHERE officiel IS NOT NULL;"


class PdfJob:
    """Conversion LibreOffice en tâche de fond. poll() : None = en cours ; Path = terminé ; PdfError = échec.

    La sortie est écrite dans un dossier temporaire puis déplacée : un PDF à moitié écrit
    (conversion interrompue) n'est donc jamais pris pour un PDF valide.
    """
    TIMEOUT = 180   # secondes ; le tout premier PDF (création du profil LibreOffice) est le plus long

    def __init__(self, soffice, odt, pdf, profile, label, timeout=None):
        self.label, self.pdf, self.finished, self._result, self._error = label, Path(pdf), False, None, None
        self.timeout = timeout or self.TIMEOUT
        self.t0 = time.monotonic()
        self.tmpdir = Path(tempfile.mkdtemp(prefix='conv_', dir=self.pdf.parent))
        self.errfile = self.tmpdir / 'stderr.txt'
        cmd = [soffice, f'-env:UserInstallation={Path(profile).resolve().as_uri()}', '--headless', '--norestore',
               '--convert-to', 'pdf', '--outdir', str(self.tmpdir), str(odt)]
        kw = {}
        if sys.platform.startswith('win'):
            kw['creationflags'] = NO_WINDOW | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
        else:
            kw['start_new_session'] = True
        LOG.info('pdf %s : démarrage', label)
        try:
            self._err = open(self.errfile, 'wb')
            self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=self._err, **kw)
        except OSError as e:
            self._finish_error(f"Impossible de lancer LibreOffice ({e.__class__.__name__}).", 'lancement', str(e))

    @classmethod
    def ready(cls, pdf, label):
        job = cls.__new__(cls)
        job.label, job.pdf, job.finished, job._result, job._error = label, Path(pdf), True, Path(pdf), None
        job.proc, job.tmpdir, job.t0, job.timeout = None, None, time.monotonic(), 0
        return job

    def _cleanup(self):
        try:
            self._err.close()
        except Exception:  # noqa: BLE001
            pass
        if self.tmpdir is not None:
            shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _finish_error(self, message, code, detail=''):
        self.finished = True
        self._error = PdfError(message)
        LOG.error('pdf %s : échec (%s) %s', self.label, code, detail[:300])
        self._cleanup()

    def cancel(self):
        if not self.finished and self.proc is not None:
            kill_tree(self.proc)
            self.finished = True
            self._error = PdfError('Création du PDF annulée.')
            LOG.warning('pdf %s : annulé', self.label)
            self._cleanup()

    def poll(self):
        if not self.finished:
            code = self.proc.poll()
            elapsed = time.monotonic() - self.t0
            if code is None:
                if elapsed > self.timeout:
                    kill_tree(self.proc)
                    self._finish_error("LibreOffice ne répond pas (délai dépassé). Réessayer ; si le problème "
                                       "persiste, voir Aide > Rapport de diagnostic.", 'delai', f'{elapsed:.0f}s')
                return None if not self.finished else self._raise()
            try:
                self._err.close()
                detail = self.errfile.read_bytes().decode('utf-8', 'replace').strip()
            except OSError:
                detail = ''
            made = self.tmpdir / (self.pdf.stem + '.pdf')
            if code == 0 and made.exists() and made.stat().st_size > 0:
                try:
                    os.replace(made, self.pdf)
                    self.finished, self._result = True, self.pdf
                    LOG.info('pdf %s : terminé en %.1f s', self.label, elapsed)
                    self._cleanup()
                except OSError as e:
                    self._finish_error("Le PDF n'a pas pu être enregistré.", 'ecriture', str(e))
            else:
                self._finish_error("La conversion en PDF a échoué.", f'code{code}', detail)
        return self._raise()

    def _raise(self):
        if self._error is not None:
            raise self._error
        return self._result


class Store:
    def __init__(self, data_dir, cfg: configparser.ConfigParser):
        self.dir = Path(data_dir)
        self.cfg = cfg
        self.doc_cfg = dict(cfg['document'])
        logo = self.doc_cfg.get('logo', '').strip()
        if logo and not Path(logo).is_absolute():
            self.doc_cfg['logo'] = str(Path(cfg_app_dir(cfg)) / logo) if cfg_app_dir(cfg) else logo
        for sub in ('archives', 'pdf', 'sauvegardes'):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.dir / 'index.sqlite3', timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        cols = [r[1] for r in self.db.execute('PRAGMA table_info(bordereaux)')]
        coltypes = {r[1]: (r[2] or '').upper() for r in self.db.execute('PRAGMA table_info(bordereaux)')}
        if 'officiel' not in coltypes:    # base de la version 0.1 / 0.2 : l'ancien « numero » était le n° officiel
            self.db.execute('ALTER TABLE bordereaux ADD COLUMN officiel TEXT')
            self.db.execute('UPDATE bordereaux SET officiel = CAST(numero AS TEXT)')
            LOG.info('migration : colonne n° officiel ajoutée')
        elif coltypes['officiel'] != 'TEXT':   # version 0.3 : n° officiel purement numérique -> texte libre
            names = ('id, annee, numero, officiel, date, destinataire, titre, observation, lignes, concernant, '
                     'en_fonction, fichier, annule, recherche, cree_le, modifie_le')
            self.db.executescript('DROP INDEX IF EXISTS ux_annee_officiel; DROP INDEX IF EXISTS ux_annee_numero; '
                                  'ALTER TABLE bordereaux RENAME TO bordereaux_v03;')
            self.db.executescript(SCHEMA)
            self.db.execute(f'INSERT INTO bordereaux ({names}) SELECT id, annee, numero, CAST(officiel AS TEXT), '
                            'date, destinataire, titre, observation, lignes, concernant, en_fonction, fichier, annule, '
                            'recherche, cree_le, modifie_le FROM bordereaux_v03')
            self.db.execute('DROP TABLE bordereaux_v03')
            LOG.info('migration : n° officiel converti en texte')
        self.db.execute(SCHEMA_OFFICIEL)
        self.db.commit()
        self._pdf_job = None
        for stale in (self.dir / 'pdf').glob('conv_*'):      # restes d'une conversion interrompue (fermeture brutale)
            shutil.rmtree(stale, ignore_errors=True)
        n =self.db.execute('SELECT COUNT(*) FROM bordereaux').fetchone()[0]
        LOG.info('ouverture du magasin : %d bordereaux dans l\'index', n)
        self.auto_backup()

    def close(self):
        if self._pdf_job is not None:
            self._pdf_job.cancel()
        self.db.close()
        LOG.info('=== fermeture ===')
        lock = getattr(self, 'lock', None)
        if lock is not None:
            lock.release()

    # ---- lecture
    def _row(self, r):
        if r is None:
            return None
        d = dict(r)
        d['lignes'] = json.loads(d['lignes'])
        return d

    def get(self, rec_id):
        return self._row(self.db.execute('SELECT * FROM bordereaux WHERE id=?', (rec_id,)).fetchone())

    def suggest_number(self, annee: int) -> int:
        r = self.db.execute('SELECT MAX(numero) FROM bordereaux WHERE annee=?', (int(annee),)).fetchone()
        return (r[0] or 0) + 1

    def years(self):
        return [r[0] for r in self.db.execute('SELECT DISTINCT annee FROM bordereaux ORDER BY annee DESC')]

    def recent_values(self, field):
        if field not in ('destinataire', 'titre'):
            raise ValueError(field)
        sql = f'SELECT {field} FROM bordereaux GROUP BY {field} ORDER BY MAX(modifie_le) DESC LIMIT 100'
        return [r[0] for r in self.db.execute(sql)]

    def search(self, text='', annee=None, include_void=False, limit=2000, sans_numero=False):
        where, params = [], []
        if annee:
            where.append('annee=?')
            params.append(int(annee))
        if not include_void:
            where.append('annule=0')
        if sans_numero:
            where.append('officiel IS NULL')
        for term in norm(text).split():
            where.append("recherche LIKE ? ESCAPE '\\'")
            params.append('%' + _like_escape(term) + '%')
        sql = 'SELECT * FROM bordereaux'
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += (' ORDER BY date ASC, numero ASC LIMIT ?' if sans_numero else ' ORDER BY date DESC, numero DESC LIMIT ?')
        params.append(int(limit))
        return [self._row(r) for r in self.db.execute(sql, params)]

    # ---- écriture
    def _dup_official(self, cur, d, exclude_id=None):
        if d['officiel'] is None:
            return
        sql = 'SELECT id, date, destinataire FROM bordereaux WHERE annee=? AND officiel=? COLLATE NOCASE'
        params = [d['annee'], d['officiel']]
        if exclude_id:
            sql += ' AND id<>?'
            params.append(exclude_id)
        dup = cur.execute(sql, params).fetchone()
        if dup:
            raise UserError('numero_existe', f"Le n° officiel {d['officiel']} est déjà attribué en {d['annee']} "
                            f"(bordereau du {odt_builder.fr_date(dup['date'])} pour {dup['destinataire']}).")

    def save(self, data: dict, edit_id=None):
        d = validate(data)
        now = dt.datetime.now().isoformat(timespec='seconds')
        old = None
        if edit_id:
            old = self.db.execute('SELECT * FROM bordereaux WHERE id=?', (edit_id,)).fetchone()
            if old is None:
                raise UserError('introuvable', "Bordereau introuvable.")
            if old['annule']:
                raise UserError('deja_annule', "Un bordereau annulé ne peut pas être modifié.")
        # référence interne : conservée à la modification (sauf changement d'année), sinon la suivante de l'année
        d['numero'] = old['numero'] if (old is not None and old['annee'] == d['annee']) else self.suggest_number(d['annee'])
        rel = f"archives/{d['annee']}/{d['numero']}_{d['date']}.odt"
        final = self.dir / rel
        final.parent.mkdir(parents=True, exist_ok=True)
        tmp = final.with_name(final.name + '.tmp')
        cur = self.db.cursor()
        old_path = None
        try:
            self._dup_official(cur, d, edit_id)
            fields = (d['annee'], d['numero'], d['officiel'], d['date'], d['destinataire'], d['titre'],
                      d['observation'], json.dumps(d['lignes'], ensure_ascii=False), d['concernant'],
                      d['en_fonction'], rel, search_text(d), now)
            if edit_id:
                cur.execute('UPDATE bordereaux SET annee=?, numero=?, officiel=?, date=?, destinataire=?, titre=?, '
                            'observation=?, lignes=?, concernant=?, en_fonction=?, fichier=?, recherche=?, modifie_le=? '
                            'WHERE id=?', fields + (edit_id,))
                rec_id = edit_id
            else:
                cur.execute('INSERT INTO bordereaux (annee, numero, officiel, date, destinataire, titre, observation, '
                            'lignes, concernant, en_fonction, fichier, recherche, cree_le, modifie_le) '
                            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', fields[:-1] + (now, now))
                rec_id = cur.lastrowid
            odt_builder.build_odt(d, self.doc_cfg, tmp)
            if old is not None:
                old_path = self.dir / old['fichier']
                if old_path.exists():
                    vdir = self.dir / 'archives' / '_versions'
                    vdir.mkdir(parents=True, exist_ok=True)
                    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
                    shutil.copy2(old_path, vdir / f'{old_path.stem}__{stamp}.odt')
                self._drop_pdf(old_path)
            os.replace(tmp, final)
            self._drop_pdf(final)
            self.db.commit()
        except BaseException as e:
            self.db.rollback()
            try:
                tmp.unlink()
            except OSError:
                pass
            if isinstance(e, UserError):
                LOG.info('enregistrement refusé : %s (%s)', e.code, ref(d['annee'], d['numero']))
            else:
                log_exception(f"enregistrement {ref(d['annee'], d['numero'])}", e)
            raise
        if old_path is not None and old_path != final and old_path.exists():
            old_path.unlink()
        LOG.info('%s : %s', 'modifié' if edit_id else 'enregistré', ref(d['annee'], d['numero']))
        return self.get(rec_id)

    def set_official(self, rec_id, value):
        """Saisit (ou retire, si vide) le n° officiel attribué par le bureau d'ordre. Le document est régénéré
        sans conserver de version intermédiaire (ce n'est pas une correction du contenu)."""
        rec = self.get(rec_id)
        if rec is None:
            raise UserError('introuvable', "Bordereau introuvable.")
        d = validate(dict(rec, date=odt_builder.fr_date(rec['date']), officiel=value), need_seq=True)
        final = self.dir / rec['fichier']
        tmp = final.with_name(final.name + '.tmp')
        cur = self.db.cursor()
        try:
            self._dup_official(cur, d, rec_id)
            cur.execute('UPDATE bordereaux SET officiel=?, recherche=?, modifie_le=? WHERE id=?',
                        (d['officiel'], search_text(d), dt.datetime.now().isoformat(timespec='seconds'), rec_id))
            odt_builder.build_odt(d, self.doc_cfg, tmp)
            os.replace(tmp, final)
            self._drop_pdf(final)
            self.db.commit()
        except BaseException as e:
            self.db.rollback()
            try:
                tmp.unlink()
            except OSError:
                pass
            if isinstance(e, UserError):
                LOG.info('n° officiel refusé : %s (%s)', e.code, ref(rec['annee'], rec['numero']))
            else:
                log_exception(f"n° officiel {ref(rec['annee'], rec['numero'])}", e)
            raise
        LOG.info('n° officiel %s : %s', 'saisi' if d['officiel'] else 'retiré', ref(rec['annee'], rec['numero']))
        return self.get(rec_id)

    def _drop_pdf(self, odt_path: Path):
        try:
            (self.dir / 'pdf' / (Path(odt_path).stem + '.pdf')).unlink()
        except OSError:
            pass

    def void(self, rec_id):
        rec = self.get(rec_id)
        if rec is None:
            raise UserError('introuvable', "Bordereau introuvable.")
        with self.db:
            self.db.execute('UPDATE bordereaux SET annule=1, modifie_le=? WHERE id=?',
                            (dt.datetime.now().isoformat(timespec='seconds'), rec_id))
        marker = (self.dir / rec['fichier']).with_suffix('.annule')
        try:
            marker.write_text('annulé', encoding='utf-8')
        except OSError:
            LOG.warning("marqueur d'annulation non écrit (%s)", ref(rec['annee'], rec['numero']))
        LOG.info('annulé : %s', ref(rec['annee'], rec['numero']))

    def doc_path(self, rec) -> Path:
        return self.dir / rec['fichier']

    # ---- liste exportée
    LIST_HEADER = ['Réf.', 'N° officiel', 'Date', 'Destinataire', 'Titre', 'Observation', 'État']
    LIST_WIDTHS = [2.4, 2.6, 2.2, 5.5, 8.0, 4.0, 2.0]       # 26,7 cm = A4 paysage

    def export_list(self, dest, text='', annee=None, include_void=False, sans_numero=False) -> int:
        """Écrit la liste des bordereaux correspondant à la recherche (du plus ancien au plus récent) dans un fichier
        .ods (LibreOffice) ou .csv (Excel / LibreOffice). Ni le tableau ni le bloc « Concernant » n'y figurent."""
        dest = Path(dest)
        recs = sorted(self.search(text, annee, include_void, limit=100000, sans_numero=sans_numero),
                      key=lambda r: (r['date'], r['annee'], r['numero']))
        rows = [[ref(r['annee'], r['numero']), r['officiel'] or '', odt_builder.fr_date(r['date']), r['destinataire'],
                 r['titre'], r['observation'], 'ANNULÉ' if r['annule'] else ''] for r in recs]
        tmp = dest.with_name(dest.name + '.tmp')
        try:
            if dest.suffix.lower() == '.csv':
                with open(tmp, 'w', encoding='utf-8-sig', newline='') as f:
                    w = csv.writer(f, delimiter=';')
                    w.writerow(self.LIST_HEADER)
                    for row in rows:      # une cellule commençant par = + - @ pourrait être prise pour une formule
                        w.writerow([("'" + v if v[:1] in ('=', '+', '-', '@') else v) for v in row])
            else:
                odt_builder.build_ods(self.LIST_HEADER, rows, self.LIST_WIDTHS, tmp)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
        LOG.info('liste exportée : %d bordereaux (%s)', len(rows), dest.suffix.lower())
        return len(rows)

    # ---- PDF
    def start_pdf(self, rec_id) -> 'PdfJob':
        """Lance la création du PDF SANS attendre : l'appelant interroge job.poll() régulièrement.

        Un seul PDF à la fois (deux LibreOffice sur le même profil se bloquent mutuellement).
        """
        if self._pdf_job is not None and not self._pdf_job.finished:
            raise UserError('pdf_en_cours', "Un PDF est déjà en cours de création. Patienter quelques secondes.")
        rec = self.get(rec_id)
        if rec is None:
            raise UserError('introuvable', "Bordereau introuvable.")
        label = ref(rec['annee'], rec['numero'])
        odt = self.doc_path(rec)
        if not odt.exists():
            LOG.error('pdf %s : fichier ODT introuvable', label)
            raise PdfError("Le fichier du document est introuvable dans les archives.")
        pdf = self.dir / 'pdf' / (odt.stem + '.pdf')
        if pdf.exists():
            job = PdfJob.ready(pdf, label)
        else:
            soffice = find_soffice(self.cfg['general'].get('libreoffice', ''))
            if not soffice:
                LOG.error('pdf %s : LibreOffice introuvable', label)
                raise PdfError("LibreOffice est introuvable sur ce poste.")
            job = PdfJob(soffice, odt, pdf, self.dir / 'lo_profile', label)
        self._pdf_job = job
        return job

    def export_pdf(self, rec_id, timeout=None) -> Path:
        """Version qui attend le résultat (tests, scripts). L'interface utilise start_pdf."""
        job = self.start_pdf(rec_id)
        while True:
            res = job.poll()
            if res is not None:
                return res
            time.sleep(0.05)

    # ---- sauvegardes
    def _snapshot_db(self, dest: Path):
        tmp = dest.with_name(dest.name + '.tmp')
        out = sqlite3.connect(tmp)
        try:
            self.db.backup(out)
        finally:
            out.close()
        os.replace(tmp, dest)

    def auto_backup(self, keep=14):
        folder = self.dir / 'sauvegardes'
        dest = folder / f'index_{dt.date.today().isoformat()}.sqlite3'
        if not dest.exists():
            self._snapshot_db(dest)
        for old in sorted(folder.glob('index_*.sqlite3'))[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    def backup_zip(self, dest) -> Path:
        dest = Path(dest)
        snap = self.dir / 'sauvegardes' / '_instantane.sqlite3'
        self._snapshot_db(snap)
        try:
            with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
                z.write(snap, 'index.sqlite3')
                for p in sorted((self.dir / 'archives').rglob('*')):
                    if p.is_file() and not p.name.endswith('.tmp') and p.resolve() != dest.resolve():
                        z.write(p, p.relative_to(self.dir).as_posix())
        finally:
            try:
                snap.unlink()
            except OSError:
                pass
        LOG.info('sauvegarde zip créée (%.1f Mo)', dest.stat().st_size / 1e6)
        return dest

    def diagnostic_zip(self, dest) -> Path:
        """Rapport à envoyer au support : journaux + fiche technique. AUCUN bordereau, aucune donnée saisie."""
        dest = Path(dest)
        n = self.db.execute('SELECT COUNT(*), COALESCE(SUM(annule),0) FROM bordereaux').fetchone()
        soffice = find_soffice(self.cfg['general'].get('libreoffice', ''))
        size = lambda p: sum(f.stat().st_size for f in Path(p).rglob('*') if f.is_file()) if Path(p).exists() else 0  # noqa: E731
        lines = [
            f'Application      : {APP_NAME} {VERSION}',
            f'Date             : {dt.datetime.now().isoformat(timespec="seconds")}',
            f'Système          : {platform.platform()}',
            f'Python           : {platform.python_version()}',
            f'Poste / session  : {platform.node()} / {os.environ.get("USERNAME") or os.environ.get("USER") or "?"}',
            f'LibreOffice      : {soffice or "INTROUVABLE"}',
            f'Dossier données  : {self.dir}',
            f'Bordereaux       : {n[0]} dans l\'index dont {n[1]} annulés',
            f'Fichiers ODT     : {len(list((self.dir / "archives").glob("[0-9][0-9][0-9][0-9]/*.odt")))}',
            f'Taille archives  : {size(self.dir / "archives") / 1e6:.1f} Mo',
            f'Taille index     : {(self.dir / "index.sqlite3").stat().st_size / 1e6:.2f} Mo',
            f'Espace disque libre : {shutil.disk_usage(self.dir).free / 1e9:.1f} Go',
            f'Profil LibreOffice : {size(self.dir / "lo_profile") / 1e6:.1f} Mo',
            f'Intégrité index  : {self.db.execute("PRAGMA integrity_check").fetchone()[0]}',
        ]
        with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('fiche_technique.txt', '\n'.join(lines) + '\n')
            for p in sorted((self.dir / 'journaux').glob('journal.log*')):
                z.write(p, f'journaux/{p.name}')
        LOG.info('rapport de diagnostic créé')
        return dest

    def rebuild_index(self):
        """Ajoute à l'index les bordereaux présents dans les archives mais absents de la base."""
        added, skipped, errors = 0, 0, []
        for odt in sorted((self.dir / 'archives').glob('[0-9][0-9][0-9][0-9]/*.odt')):
            raw = odt_builder.read_data(odt)
            if raw is None:
                errors.append(f'{odt.name} : données illisibles')
                continue
            try:
                d = validate(raw, need_seq=True)
                if 'officiel' not in raw:          # fichier d'une ancienne version : « numero » était le n° officiel
                    d['officiel'] = str(d['numero'])
            except ValueError as e:
                errors.append(f'{odt.name} : {e}')
                continue
            exists = self.db.execute('SELECT 1 FROM bordereaux WHERE annee=? AND numero=?',
                                     (d['annee'], d['numero'])).fetchone()
            if exists:
                skipped += 1
                continue
            now = dt.datetime.now().isoformat(timespec='seconds')
            rel = odt.relative_to(self.dir).as_posix()
            void = 1 if odt.with_suffix('.annule').exists() else 0
            try:
                with self.db:
                    self.db.execute(
                        'INSERT INTO bordereaux (annee, numero, officiel, date, destinataire, titre, observation, lignes, '
                        'concernant, en_fonction, fichier, annule, recherche, cree_le, modifie_le) '
                        'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (d['annee'], d['numero'], d['officiel'], d['date'], d['destinataire'], d['titre'],
                         d['observation'], json.dumps(d['lignes'], ensure_ascii=False), d['concernant'],
                         d['en_fonction'], rel, void, search_text(d), now, now))
            except sqlite3.IntegrityError:
                errors.append(f'{odt.name} : n° officiel déjà utilisé par un autre bordereau')
                continue
            added += 1
        LOG.info('reconstruction : %d ajoutés, %d déjà présents, %d erreurs', added, skipped, len(errors))
        return {'ajoutes': added, 'deja_presents': skipped, 'erreurs': errors}


def cfg_app_dir(cfg) -> str:
    return getattr(cfg, '_app_dir', '')


def open_store(app_dir, data_dir_override=None) -> Store:
    cfg = load_config(app_dir)
    cfg._app_dir = str(app_dir)  # type: ignore[attr-defined]
    chosen = Path(data_dir_override or cfg['general'].get('dossier_donnees', '').strip() or default_data_dir())
    chosen.mkdir(parents=True, exist_ok=True)
    lock = SingleInstance(chosen)
    if not lock.acquire():
        raise AlreadyRunning()
    try:
        setup_logging(chosen)
        LOG.info('=== démarrage %s %s | Python %s | %s ===', APP_NAME, VERSION, platform.python_version(),
                 platform.platform())
        store = Store(chosen, cfg)
    except BaseException:
        lock.release()
        raise
    store.lock = lock
    return store
