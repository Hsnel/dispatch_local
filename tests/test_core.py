# -*- coding: utf-8 -*-
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
import zlib
from pathlib import Path
from xml.dom import minidom

HERE = Path(__file__).resolve().parent
APP = HERE.parent if (HERE.parent / 'core.py').exists() else HERE.parent / 'Bordereaux'
sys.path.insert(0, str(APP))
import core  # noqa: E402
import odt_builder  # noqa: E402

LOG = core.LOG


def can_render():
    """Les tests de rendu demandent LibreOffice et pdftotext (poppler)."""
    return bool(core.find_soffice() and shutil.which('pdftotext') and shutil.which('pdfinfo'))


def sample(**kw):
    d = dict(
        officiel=116, date='22/10/2026', destinataire='Mme. La Directrice de la DRH',
        titre="Dossier d'accident du travail du 06/12/2025", observation='Soit transmis à la DRH',
        lignes=[["Déclaration d'accident de travail", '02'], ['C.R de cessation de service', '02'],
                ['Certificat de constatation', '03'], ['Copie CIN', '01'], ["Fiche d'Admission Urgences", '01']],
        concernant='Mme : TEST Exemple\nGrade : Infirmière de 1er grade\nPPR : 000000\nCIN : XX000000',
        en_fonction='Service des Urgences')
    d.update(kw)
    return d


def make_png(path, w=600, h=120):
    raw = b''.join(b'\x00' + b'\x20\x40\x80' * w for _ in range(h))

    def chunk(t, data):
        c = struct.pack('>I', len(data)) + t + data
        return c + struct.pack('>I', zlib.crc32(t + data) & 0xffffffff)
    png = (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0))
           + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))
    Path(path).write_bytes(png)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='bdx_'))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def store(self, **doc):
        cfg = core.load_config(self.tmp / 'noconfig')
        for k, v in doc.items():
            cfg['document'][k] = v
        cfg._app_dir = str(self.tmp)
        s = core.Store(self.tmp / 'data', cfg)
        self.addCleanup(s.close)
        return s


class TestCore(Base):
    def test_config_read_in_utf8_or_ansi_and_defaults_when_missing(self):
        ini = "[document]\npied_ligne1 = Rue de l'Hôpital, Casablanca\nreference = N° {numero} X\n"
        for enc in ('utf-8', 'utf-8-sig', 'cp1252'):
            (self.tmp / 'config.ini').write_bytes(ini.encode(enc))
            cfg = core.load_config(self.tmp)
            self.assertEqual(cfg['document']['pied_ligne1'], "Rue de l'Hôpital, Casablanca", enc)
            self.assertEqual(cfg['document']['reference'], 'N° {numero} X', enc)
            self.assertEqual(cfg['document']['colonne_nombre'], 'Nbr')
        (self.tmp / 'config.ini').unlink()
        self.assertEqual(core.load_config(self.tmp)['document']['signataire'], 'Le Directeur')

    def test_save_creates_file_and_row(self):
        s = self.store()
        self.assertEqual(s.suggest_number(2026), 1)
        rec = s.save(sample())
        self.assertTrue((s.dir / rec['fichier']).is_file())
        self.assertEqual(rec['fichier'], 'archives/2026/1_2026-10-22.odt')     # référence interne, pas le n° officiel
        self.assertEqual((rec['numero'], rec['officiel'], core.ref(rec['annee'], rec['numero'])), (1, '116', '2026-0001'))
        self.assertEqual(s.suggest_number(2026), 2)
        self.assertEqual(s.suggest_number(2027), 1)

    def test_duplicate_number_refused_and_nothing_written(self):
        s = self.store()
        s.save(sample())
        with self.assertRaises(ValueError) as cm:
            s.save(sample(date='23/10/2026', titre='Autre'))
        self.assertIn('déjà attribué', str(cm.exception))
        self.assertEqual(len(s.search()), 1)
        self.assertEqual(len(list((s.dir / 'archives' / '2026').glob('*'))), 1)

    def test_validation(self):
        s = self.store()
        for bad, msg in [(dict(officiel='a;b'), 'seulement'), (dict(officiel='<x>'), 'seulement'),
                         (dict(officiel='1' * 31), 'seulement'), (dict(date='31/02/2026'), 'Date invalide'),
                         (dict(destinataire='  '), 'obligatoire'), (dict(titre=''), 'obligatoire'),
                         (dict(lignes=[['', '3']]), 'désignation est vide'), (dict(titre='x' * 400), 'trop long')]:
            with self.assertRaises(ValueError, msg=str(bad)) as cm:
                s.save(sample(**bad))
            self.assertIn(msg, str(cm.exception))
        self.assertEqual(s.search(), [])

    def test_empty_lines_ignored_and_no_lines_allowed(self):
        s = self.store()
        rec = s.save(sample(lignes=[['', ''], ['Pièce A', ''], ['', '']], concernant='', en_fonction=''))
        self.assertEqual(rec['lignes'], [['Pièce A', '']])
        rec2 = s.save(sample(officiel=117, lignes=[], concernant='', en_fonction=''))
        self.assertEqual(rec2['lignes'], [])

    def test_search_accents_terms_year_void(self):
        s = self.store()
        s.save(sample(officiel=1, date='08/01/2026', destinataire='Monsieur le Délégué Provincial', titre='Demandes de mutation'))
        s.save(sample(officiel=2, date='09/01/2027', destinataire='Mme la Directrice', titre='Factures pharmacie'))
        self.assertEqual([r['officiel'] for r in s.search('delegue')], ['1'])
        self.assertEqual([r['officiel'] for r in s.search('DÉLÉGUÉ mutation')], ['1'])
        self.assertEqual(s.search('delegue facture'), [])
        self.assertEqual([r['officiel'] for r in s.search('', annee=2027)], ['2'])
        self.assertEqual([r['officiel'] for r in s.search('test exemple')], ['2', '1'])  # « Concernant » est cherché ; plus récent d'abord
        self.assertEqual([r['officiel'] for r in s.search('09/01/2027')], ['2'])
        self.assertEqual(s.search('100%'), [])      # % et _ ne sont pas des jokers
        self.assertEqual(s.search("'; DROP TABLE bordereaux; --"), [])
        s.void(s.search('delegue')[0]['id'])
        self.assertEqual(s.search('delegue'), [])
        self.assertEqual(len(s.search('delegue', include_void=True)), 1)
        self.assertEqual(s.suggest_number(2026), 2)   # un numéro annulé reste occupé
        with self.assertRaises(ValueError):
            s.save(sample(officiel=1, date='08/01/2026'), edit_id=None)

    def test_edit_keeps_previous_version_and_can_move_year(self):
        s = self.store()
        rec = s.save(sample())
        rec2 = s.save(sample(titre='Titre corrigé'), edit_id=rec['id'])
        self.assertEqual(rec2['titre'], 'Titre corrigé')
        self.assertEqual(len(list((s.dir / 'archives' / '_versions').glob('*.odt'))), 1)
        self.assertEqual((rec2['numero'], rec2['officiel']), (1, '116'))        # la référence interne ne change pas
        rec3 = s.save(sample(officiel=5, date='03/01/2027'), edit_id=rec['id'])
        self.assertEqual(rec3['fichier'], 'archives/2027/1_2027-01-03.odt')    # nouvelle année : nouvelle séquence
        self.assertFalse((s.dir / 'archives/2026/1_2026-10-22.odt').exists())
        self.assertEqual(len(s.search()), 1)
        s.void(rec3['id'])
        with self.assertRaises(ValueError):
            s.save(sample(officiel=5, date='03/01/2027'), edit_id=rec3['id'])

    def test_failed_build_leaves_no_trace(self):
        s = self.store(logo=str(self.tmp / 'absent.png'))
        with self.assertRaises(ValueError):
            s.save(sample())
        self.assertEqual(s.search(), [])
        self.assertEqual(list((s.dir / 'archives').rglob('*.odt*')), [])

    def test_rebuild_index_from_files_keeps_void(self):
        s = self.store()
        a = s.save(sample(officiel=11, date='01/02/2026'))
        s.save(sample(officiel=None, date='02/02/2026', titre='Deux & <trois> "quatre"'))
        s.void(a['id'])
        s.close()
        os.remove(s.dir / 'index.sqlite3')
        s2 = self.store()
        info = s2.rebuild_index()
        self.assertEqual(info['ajoutes'], 2)
        self.assertEqual(info['erreurs'], [])
        self.assertEqual(len(s2.search(include_void=True)), 2)
        self.assertEqual([(r['numero'], r['officiel']) for r in s2.search()], [(2, None)])
        self.assertEqual([r['officiel'] for r in s2.search(include_void=True) if r['numero'] == 1], ['11'])
        self.assertEqual(s2.search()[0]['titre'], 'Deux & <trois> "quatre"')
        self.assertEqual(s2.rebuild_index()['ajoutes'], 0)

    def test_backups(self):
        s = self.store()
        s.save(sample())
        self.assertEqual(len(list((s.dir / 'sauvegardes').glob('index_*.sqlite3'))), 1)
        for i in range(20):
            (s.dir / 'sauvegardes' / f'index_2020-01-{i + 1:02d}.sqlite3').write_bytes(b'x')
        s.auto_backup()
        self.assertLessEqual(len(list((s.dir / 'sauvegardes').glob('index_*.sqlite3'))), 14)
        out = s.backup_zip(self.tmp / 'copie.zip')
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
        self.assertIn('index.sqlite3', names)
        self.assertIn('archives/2026/1_2026-10-22.odt', names)
        self.assertFalse((s.dir / 'sauvegardes' / '_instantane.sqlite3').exists())


class TestDocument(Base):
    def test_all_parts_are_well_formed_xml_and_special_chars_survive(self):
        s = self.store(pied_ligne1='Adresse & <test>', pied_ligne2='Tél : 00')
        nasty = 'A & B <c> "d" \'e\' \x00\x07 fin'
        rec = s.save(sample(destinataire=nasty, titre=nasty, observation=nasty, lignes=[[nasty, '1']],
                            concernant=nasty, en_fonction=nasty))
        with zipfile.ZipFile(s.doc_path(rec)) as z:
            self.assertEqual(z.namelist()[0], 'mimetype')
            self.assertEqual(z.getinfo('mimetype').compress_type, zipfile.ZIP_STORED)
            for n in ('content.xml', 'styles.xml', 'meta.xml', 'META-INF/manifest.xml'):
                minidom.parseString(z.read(n))
        back = odt_builder.read_data(s.doc_path(rec))
        self.assertEqual(back['titre'], 'A & B <c> "d" \'e\' fin')

    def test_renders_with_libreoffice_and_matches_layout(self):
        if not can_render():
            self.skipTest('LibreOffice absent')
        png = self.tmp / 'logo.png'
        make_png(png)
        s = self.store(logo=str(png), pied_ligne1="Adresse de l'établissement", pied_ligne2='Tél : 00 00 00 00 00')
        rec = s.save(sample())
        pdf = s.export_pdf(rec['id'])
        text = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True).stdout
        for needle in ('N° 116 S.P.A.G/BRH', 'Le Directeur', '22/10/2026', 'Mme. La Directrice de la DRH',
                       "Bordereau d'envoi", 'DESIGNATION', 'Nbr', 'OBSERVATION', "Dossier d'accident du travail du 06/12/2025",
                       "Déclaration d'accident de travail", 'Concernant', 'PPR : 000000', 'En fonction à : Service des Urgences',
                       '« Soit transmis à la DRH »', "Adresse de l'établissement"):
            self.assertIn(needle, text)
        pages = int([l for l in subprocess.run(['pdfinfo', str(pdf)], capture_output=True, text=True).stdout.splitlines()
                     if l.startswith('Pages')][0].split()[1])
        self.assertEqual(pages, 1)
        shutil.copy(pdf, os.environ.get('BDX_SAVE_PDF', os.devnull)) if os.environ.get('BDX_SAVE_PDF') else None

    def test_long_list_spans_pages_with_repeating_header(self):
        if not can_render():
            self.skipTest('LibreOffice absent')
        lignes = [[f'Pièce numéro {i} ' + 'très longue désignation ' * (i % 4), str(i % 5 + 1)] for i in range(1, 61)]
        s = self.store()
        rec = s.save(sample(lignes=lignes))
        pdf = s.export_pdf(rec['id'])
        out = subprocess.run(['pdftotext', '-layout', str(pdf), '-'], capture_output=True, text=True).stdout
        pages = out.count('\f')
        self.assertGreaterEqual(pages, 2)
        self.assertEqual(out.count('DESIGNATION'), pages)   # en-tête du tableau répété sur chaque page
        self.assertIn('Pièce numéro 60', out)

    def test_pdf_is_cached_then_regenerated_after_edit(self):
        if not can_render():
            self.skipTest('LibreOffice absent')
        s = self.store()
        rec = s.save(sample())
        p1 = s.export_pdf(rec['id'])
        m1 = p1.stat().st_mtime_ns
        self.assertEqual(s.export_pdf(rec['id']).stat().st_mtime_ns, m1)
        s.save(sample(titre='Titre modifié'), edit_id=rec['id'])
        p2 = s.export_pdf(rec['id'])
        out = subprocess.run(['pdftotext', str(p2), '-'], capture_output=True, text=True).stdout
        self.assertIn('Titre modifié', out)

    def test_logo_png_and_bad_logo(self):
        png = self.tmp / 'logo.png'
        make_png(png, 1000, 100)
        info = odt_builder.load_logo(str(png))
        self.assertAlmostEqual(info['w'], 17.0, places=1)
        self.assertAlmostEqual(info['h'], 1.7, places=1)
        bad = self.tmp / 'bad.png'
        bad.write_bytes(b'pas une image')
        with self.assertRaises(ValueError):
            odt_builder.load_logo(str(bad))
        tall = self.tmp / 'tall.png'
        make_png(tall, 100, 400)
        t = odt_builder.load_logo(str(tall))
        self.assertLessEqual(t['h'], 3.2001)

    def test_logo_height_limit_is_configurable(self):
        wide = self.tmp / 'wide.png'
        make_png(wide, 1000, 300)              # 17 cm de large donnent 5,1 cm de haut
        small = odt_builder.load_logo(str(wide))                      # ancienne limite 3,2 cm : image réduite
        self.assertAlmostEqual(small['h'], 3.2, places=1)
        self.assertLess(small['w'], 11)
        s = self.store(logo=str(wide))                                # nouvelle limite par défaut 6 cm : pleine largeur
        rec = s.save(sample())
        info = odt_builder.load_logo(str(wide), 6.0)
        self.assertAlmostEqual(info['w'], 17.0, places=1)
        with zipfile.ZipFile(s.doc_path(rec)) as z:
            self.assertIn('svg:width="17.00cm"', z.read('styles.xml').decode())
        s2 = self.store(logo=str(wide), logo_hauteur_max='abc')       # valeur invalide : retombe sur 6 cm
        s2.save(sample(officiel=5))

    def test_jpeg_size_parser(self):
        # JPEG minimal : SOI + SOF0 (hauteur 50, largeur 200)
        data = b'\xff\xd8' + b'\xff\xc0' + struct.pack('>HBHHB', 11, 8, 50, 200, 1) + b'\x01\x11\x00' + b'\xff\xd9'
        w, h, ext, mime = odt_builder.image_info(data)
        self.assertEqual((w, h, ext, mime), (200, 50, 'jpg', 'image/jpeg'))


PERSONAL = ['TEST Exemple', 'Directrice', 'Infirmière', 'XX000000', "Dossier d'accident", 'Urgences', 'Soit transmis',
            'Certificat de constatation', '000000']


def fake_soffice(folder, body):
    """Faux LibreOffice (script shell) pour tester délai dépassé / échec sans dépendre du vrai programme."""
    path = Path(folder) / 'fake_soffice.sh'
    path.write_text('#!/bin/sh\n' + body + '\n')
    path.chmod(0o755)
    return str(path)


class TestExport(Base):
    def make(self):
        s = self.store()
        a = s.save(sample(date='20/10/2026', officiel='0042', destinataire='Dest & <A>'))
        b = s.save(sample(date='05/10/2026', officiel='-5', titre='=SOMME(1;2)'))
        c = s.save(sample(date='22/10/2026', officiel=None, titre='Sans numéro'))
        d = s.save(sample(date='01/01/2027', officiel=None, titre='Année suivante'))
        s.void(c['id'])
        return s, (a, b, c, d)

    def read_ods_rows(self, path):
        with zipfile.ZipFile(path) as z:
            self.assertEqual(z.namelist()[0], 'mimetype')
            for n in ('content.xml', 'styles.xml', 'META-INF/manifest.xml'):
                minidom.parseString(z.read(n))
            doc = minidom.parseString(z.read('content.xml'))
        rows = []
        for tr in doc.getElementsByTagName('table:table-row'):
            rows.append([''.join(t.firstChild.data for t in c.getElementsByTagName('text:p') if t.firstChild)
                         for c in tr.getElementsByTagName('table:table-cell')])
        return rows

    def test_ods_export_order_columns_and_no_personal_data(self):
        s, (a, b, c, d) = self.make()
        out = self.tmp / 'liste.ods'
        self.assertEqual(s.export_list(out), 3)                       # annulé exclu par défaut
        rows = self.read_ods_rows(out)
        self.assertEqual(rows[0], core.Store.LIST_HEADER)
        self.assertEqual([r[0] for r in rows[1:]], ['2026-0002', '2026-0001', '2027-0001'])   # plus ancien d'abord
        self.assertEqual(rows[1][1:3], ['-5', '05/10/2026'])
        self.assertEqual(rows[2][1], '0042')                           # texte : le zéro reste
        self.assertEqual(rows[2][3], 'Dest & <A>')
        blob = out.read_bytes()
        with zipfile.ZipFile(out) as z:
            blob = z.read('content.xml').decode()
        for bad in ('TEST Exemple', 'XX000000', 'Infirmière', "Déclaration d'accident"):
            self.assertNotIn(bad, blob)                                # ni « Concernant » ni le tableau
        self.assertFalse(list(self.tmp.glob('*.tmp')))

    def test_filters_follow_the_search(self):
        s, (a, b, c, d) = self.make()
        out = self.tmp / 'x.ods'
        self.assertEqual(s.export_list(out, include_void=True), 4)
        self.assertEqual(s.export_list(out, annee=2027), 1)
        self.assertEqual(s.export_list(out, sans_numero=True), 1)      # l'annulé n'est pas compté, 2027 oui
        self.assertEqual(s.export_list(out, text='somme'), 1)
        self.assertEqual(s.export_list(out, text='introuvable'), 0)

    def test_csv_export_excel_friendly_and_formula_safe(self):
        s, _ = self.make()
        out = self.tmp / 'liste.csv'
        s.export_list(out, include_void=True)
        raw = out.read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))              # BOM : accents corrects dans Excel
        lines = raw.decode('utf-8-sig').splitlines()
        self.assertEqual(lines[0], ';'.join(core.Store.LIST_HEADER))
        self.assertIn("2026-0002;'-5;05/10/2026;", lines[1])
        self.assertIn("\"'=SOMME(1;2)\"", lines[1])                    # jamais une formule
        self.assertTrue(lines[3].endswith('ANNULÉ'))

    def test_libreoffice_can_open_the_list(self):
        if not can_render():
            self.skipTest('LibreOffice absent')
        s, _ = self.make()
        out = self.tmp / 'liste.ods'
        s.export_list(out)
        conv = self.tmp / 'conv'
        conv.mkdir()
        subprocess.run([core.find_soffice(), f'-env:UserInstallation={(self.tmp / "p").resolve().as_uri()}', '--headless',
                        '--convert-to', 'csv:Text - txt - csv (StarCalc):59,34,76,1', '--outdir', str(conv), str(out)],
                       capture_output=True, timeout=180)
        text = (conv / 'liste.csv').read_text(encoding='utf-8')
        self.assertIn('"2026-0001";"0042";"20/10/2026";"Dest & <A>"', text)
        self.assertIn('"=SOMME(1;2)"', text)                           # reste du texte, pas une formule


class TestOfficialNumber(Base):
    def test_blank_dots_until_official_number_is_entered(self):
        s = self.store()
        rec = s.save(sample(officiel=None))
        self.assertIsNone(rec['officiel'])
        with zipfile.ZipFile(s.doc_path(rec)) as z:
            self.assertIn('N° .......... S.P.A.G/BRH', z.read('content.xml').decode())
        self.assertEqual([r['id'] for r in s.search(sans_numero=True)], [rec['id']])
        rec2 = s.set_official(rec['id'], '212')
        self.assertEqual(rec2['officiel'], '212')
        with zipfile.ZipFile(s.doc_path(rec2)) as z:
            self.assertIn('N° 212 S.P.A.G/BRH', z.read('content.xml').decode())
        self.assertEqual(odt_builder.read_data(s.doc_path(rec2))['officiel'], '212')
        self.assertEqual(s.search(sans_numero=True), [])
        self.assertEqual([r['id'] for r in s.search('212')], [rec['id']])
        self.assertEqual([r['id'] for r in s.search('2026-0001')], [rec['id']])
        self.assertFalse((s.dir / 'archives' / '_versions').exists())          # pas de version intermédiaire
        self.assertEqual(s.set_official(rec['id'], '')['officiel'], None)       # retrait possible

    def test_official_number_unique_per_year_and_validated(self):
        s = self.store()
        a = s.save(sample(officiel=None))
        b = s.save(sample(officiel=None, titre='Autre'))
        s.set_official(a['id'], '10')
        with self.assertRaises(core.UserError) as cm:
            s.set_official(b['id'], '10')
        self.assertEqual(cm.exception.code, 'numero_existe')
        for bad in ('a;b', '<script>', '1' * 31, "x'; DROP TABLE bordereaux; --"):
            with self.assertRaises(core.UserError):
                s.set_official(b['id'], bad)
        self.assertIsNone(s.get(b['id'])['officiel'])
        c = s.save(sample(officiel=None, date='05/01/2027'))
        self.assertEqual(s.set_official(c['id'], '10')['officiel'], '10')         # autre année : autorisé
        self.assertEqual(s.save(sample(titre='Retouché', officiel='10'), edit_id=a['id'])['officiel'], '10')

    def test_official_number_accepts_text_formats_and_is_unique_ignoring_case(self):
        s = self.store()
        recs = [s.save(sample(officiel=None, titre=f'T{i}')) for i in range(4)]
        for rec, val in zip(recs, ('116/2026', '116 bis', ' BO-12.3  ', '0042')):
            got = s.set_official(rec['id'], val)
        self.assertEqual([s.get(r['id'])['officiel'] for r in recs], ['116/2026', '116 bis', 'BO-12.3', '0042'])  # 0042 garde son zéro
        with self.assertRaises(core.UserError):
            s.set_official(recs[0]['id'], '116  BIS')                           # même numéro à la casse et aux espaces près
        with zipfile.ZipFile(s.doc_path(s.get(recs[0]['id']))) as z:
            self.assertIn('N° 116/2026 S.P.A.G/BRH', z.read('content.xml').decode())
        self.assertEqual([r['id'] for r in s.search('116 bis')], [recs[1]['id']])

    def test_migration_from_version_0_3_integer_column(self):
        import sqlite3
        data = self.tmp / 'data'
        (data / 'archives' / '2026').mkdir(parents=True)
        db = sqlite3.connect(data / 'index.sqlite3')
        db.executescript(core.SCHEMA.replace('officiel TEXT', 'officiel INTEGER'))
        db.execute("CREATE UNIQUE INDEX ux_annee_officiel ON bordereaux(annee, officiel) WHERE officiel IS NOT NULL")
        db.execute("INSERT INTO bordereaux (annee, numero, officiel, date, destinataire, titre, observation, lignes, "
                   "concernant, en_fonction, fichier, recherche, cree_le, modifie_le) VALUES (2026, 3, 212, '2026-10-22', "
                   "'D', 'T', '', '[]', '', '', 'archives/2026/3_2026-10-22.odt', 'd t', 'x', 'x')")
        db.commit(); db.close()
        s = self.store()
        r = s.search()[0]
        self.assertEqual((r['numero'], r['officiel']), (3, '212'))
        self.assertEqual(s.db.execute("SELECT typeof(officiel) FROM bordereaux").fetchone()[0], 'text')
        rec = s.save(sample(officiel=None))
        self.assertEqual(s.set_official(rec['id'], '212/2026')['officiel'], '212/2026')
        with self.assertRaises(core.UserError):
            s.set_official(s.save(sample(officiel=None))['id'], '212')

    def test_edit_keeps_official_number_and_internal_ref(self):
        s = self.store()
        a = s.save(sample(officiel=None))
        s.save(sample(officiel=None, titre='Deux'))
        s.set_official(a['id'], '50')
        e = s.save(sample(titre='Corrigé', officiel=s.get(a['id'])['officiel']), edit_id=a['id'])
        self.assertEqual((e['numero'], e['officiel']), (1, '50'))

    def test_internal_numbers_are_sequential_per_year_and_voided_stay_used(self):
        s = self.store()
        r1 = s.save(sample(officiel=None))
        s.void(r1['id'])
        r2 = s.save(sample(officiel=None))
        r3 = s.save(sample(officiel=None, date='02/01/2027'))
        self.assertEqual((r1['numero'], r2['numero'], r3['numero']), (1, 2, 1))

    def test_migration_from_previous_database_keeps_old_numbers_as_official(self):
        data = self.tmp / 'data'
        (data / 'archives' / '2026').mkdir(parents=True)
        import sqlite3
        db = sqlite3.connect(data / 'index.sqlite3')
        old_schema = core.SCHEMA.replace('  officiel TEXT,\n', '')
        db.executescript(old_schema)
        db.execute("INSERT INTO bordereaux (annee, numero, date, destinataire, titre, observation, lignes, concernant, "
                   "en_fonction, fichier, recherche, cree_le, modifie_le) VALUES (2026, 116, '2026-10-22', 'D', 'T', '', "
                   "'[]', '', '', 'archives/2026/116_2026-10-22.odt', 'd t', 'x', 'x')")
        db.commit()
        db.close()
        s = self.store()
        r = s.search()[0]
        self.assertEqual((r['numero'], r['officiel']), (116, '116'))
        self.assertEqual(s.suggest_number(2026), 117)
        self.assertEqual(s.save(sample(officiel=None))['numero'], 117)

    def test_rebuild_reads_old_files_where_numero_was_the_official_number(self):
        s = self.store()
        rec = s.save(sample(officiel=None))
        path = s.doc_path(rec)
        # fabrique un fichier « version 1 » : pas de clé officiel, numero = n° officiel
        data = odt_builder.read_data(path)
        data.pop('officiel'); data['numero'] = 77
        old = dict(data)
        old.update(lignes=data['lignes'])
        v1 = self.tmp / 'v1.odt'
        d = core.validate(dict(old, date=odt_builder.fr_date(old['date'])), need_seq=True)
        odt_builder.build_odt(d, s.doc_cfg, v1)
        with zipfile.ZipFile(v1) as zin:
            parts = {n: zin.read(n) for n in zin.namelist()}
        meta = parts['meta.xml'].decode().replace('&quot;officiel&quot;:null,', '').replace('"officiel":null,', '')
        parts['meta.xml'] = meta.encode()
        new = s.dir / 'archives' / '2026' / '77_2026-10-22.odt'
        with zipfile.ZipFile(new, 'w') as zo:
            zo.writestr(zipfile.ZipInfo('mimetype'), parts.pop('mimetype'), compress_type=zipfile.ZIP_STORED)
            for n, b in parts.items():
                zo.writestr(n, b, compress_type=zipfile.ZIP_DEFLATED)
        self.assertNotIn('officiel', odt_builder.read_data(new))
        info = s.rebuild_index()
        self.assertEqual(info['erreurs'], [])
        self.assertEqual(info['ajoutes'], 1)
        got = [r for r in s.search() if r['numero'] == 77][0]
        self.assertEqual(got['officiel'], '77')

    def test_footer_third_line_and_blank_lines_before_date(self):
        s = self.store(pied_ligne1='L1', pied_ligne2='L2', pied_ligne3='L3 troisième', lignes_vides_avant_date='2')
        rec = s.save(sample())
        with zipfile.ZipFile(s.doc_path(rec)) as z:
            self.assertIn('L3 troisième', z.read('styles.xml').decode())
            c = z.read('content.xml').decode()
        self.assertEqual(c.count('<text:p text:style-name="Bdx_Ref"/>'), 2)


@unittest.skipIf(sys.platform.startswith('win'), 'faux LibreOffice en script shell')
class TestRobustness(Base):
    def wait(self, job, limit=60):
        t = time.time()
        while time.time() - t < limit:
            try:
                r = job.poll()
            except core.PdfError as e:
                return e
            if r is not None:
                return r
            time.sleep(0.05)
        self.fail('job jamais terminé')

    def test_timeout_kills_process_and_leaves_no_trace(self):
        s = self.store(); s.cfg['general']['libreoffice'] = fake_soffice(self.tmp, 'sleep 60 & sleep 60')
        rec = s.save(sample())
        core.PdfJob.TIMEOUT = 1
        self.addCleanup(setattr, core.PdfJob, 'TIMEOUT', 180)
        job = s.start_pdf(rec['id'])
        t = time.time()
        res = self.wait(job)
        self.assertIsInstance(res, core.PdfError)
        self.assertLess(time.time() - t, 10)
        self.assertEqual(list((s.dir / 'pdf').iterdir()), [])          # ni PDF à moitié écrit, ni dossier temporaire
        self.assertIsNotNone(job.proc.poll())                          # processus arrêté
        s.start_pdf(rec['id'])                                         # un nouvel essai est possible

    def test_failure_is_reported_and_not_cached(self):
        s = self.store(); s.cfg['general']['libreoffice'] = fake_soffice(self.tmp, 'echo boom >&2; exit 3')
        rec = s.save(sample())
        self.assertIsInstance(self.wait(s.start_pdf(rec['id'])), core.PdfError)
        self.assertEqual(list((s.dir / 'pdf').iterdir()), [])

    def test_only_one_pdf_at_a_time_and_cancel(self):
        s = self.store(); s.cfg['general']['libreoffice'] = fake_soffice(self.tmp, 'sleep 30')
        rec = s.save(sample())
        job = s.start_pdf(rec['id'])
        with self.assertRaises(core.UserError) as cm:
            s.start_pdf(rec['id'])
        self.assertEqual(cm.exception.code, 'pdf_en_cours')
        t = time.time()
        job.cancel()
        self.assertLess(time.time() - t, 5)
        self.assertEqual(list((s.dir / 'pdf').iterdir()), [])
        s.start_pdf(rec['id']).cancel()

    def test_start_returns_immediately_and_real_pdf_arrives_later(self):
        if not can_render():
            self.skipTest('LibreOffice absent')
        s = self.store()
        rec = s.save(sample())
        t = time.time()
        job = s.start_pdf(rec['id'])
        self.assertLess(time.time() - t, 1.0)                          # l'appel ne bloque pas
        self.assertIsNone(job.poll())                                  # encore en cours juste après
        pdf = self.wait(job, 120)
        self.assertTrue(pdf.exists() and pdf.stat().st_size > 1000)
        self.assertEqual(s.start_pdf(rec['id']).poll(), pdf)           # déjà fait : résultat immédiat

    def test_single_instance(self):
        a, b = core.SingleInstance(self.tmp), core.SingleInstance(self.tmp)
        self.assertTrue(a.acquire())
        self.assertFalse(b.acquire())
        a.release()
        self.assertTrue(b.acquire())
        b.release()

    def test_single_instance_across_processes(self):
        a = core.SingleInstance(self.tmp)
        self.assertTrue(a.acquire())
        code = ('import sys; sys.path.insert(0, sys.argv[1]); import core; '
                'print(core.SingleInstance(sys.argv[2]).acquire())')
        out = subprocess.run([sys.executable, '-I', '-c', code, str(APP), str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), 'False')
        a.release()
        out = subprocess.run([sys.executable, '-I', '-c', code, str(APP), str(self.tmp)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), 'True')

    def run_everything(self):
        s = self.store()
        core.setup_logging(s.dir)
        self.addCleanup(lambda: [LOG.removeHandler(h) or h.close() for h in list(LOG.handlers)])
        rec = s.save(sample())
        with self.assertRaises(core.UserError):
            s.save(sample())                                           # doublon
        with self.assertRaises(core.UserError):
            s.save(sample(officiel='a;b'))
        s.save(sample(officiel=117, titre='Autre titre secret'), edit_id=None)
        s.search('Infirmière TEST')
        s.void(rec['id'])
        s.backup_zip(self.tmp / 'b.zip')
        s.rebuild_index()
        s.cfg['general']['libreoffice'] = fake_soffice(self.tmp, 'exit 1')
        rec2 = s.get(2)
        try:
            self.wait(s.start_pdf(rec2['id']))
        except core.PdfError:
            pass
        try:
            raise ValueError("Mme TEST Exemple CIN XX000000")
        except ValueError as e:
            core.log_exception('test', e)
        return s

    def test_journal_never_contains_personal_data(self):
        s = self.run_everything()
        text = ''.join(p.read_text(encoding='utf-8') for p in (s.dir / 'journaux').glob('journal.log*'))
        self.assertIn('enregistré : 2026-0001', text)
        self.assertIn('annulé : 2026-0001', text)
        self.assertIn('numero_existe', text)
        self.assertIn('ValueError', text)                              # le type de l'erreur est noté ...
        for bad in PERSONAL + ['Autre titre secret', 'a;b']:
            self.assertNotIn(bad, text, bad)                           # ... jamais son message ni les saisies

    def test_diagnostic_zip_has_no_documents(self):
        s = self.run_everything()
        z = s.diagnostic_zip(self.tmp / 'diag.zip')
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            blob = ''.join(zf.read(n).decode('utf-8', 'replace') for n in names)
        self.assertEqual(names[0], 'fiche_technique.txt')
        self.assertTrue(all(n == 'fiche_technique.txt' or n.startswith('journaux/') for n in names), names)
        self.assertIn('Bordereaux       : 2 dans l', blob)
        self.assertIn('ok', blob)                                      # contrôle d'intégrité de l'index
        for bad in PERSONAL + ['Autre titre secret']:
            self.assertNotIn(bad, blob, bad)

    def test_journal_is_rotated(self):
        s = self.store()
        folder = core.setup_logging(s.dir)
        self.addCleanup(lambda: [LOG.removeHandler(h) or h.close() for h in list(LOG.handlers)])
        for i in range(6000):
            LOG.info('ligne de remplissage %05d ' + 'x' * 80, i)
        files = list(folder.glob('journal.log*'))
        self.assertGreater(len(files), 1)
        self.assertLessEqual(len(files), 5)
        self.assertLess(sum(f.stat().st_size for f in files), 2_600_000)


class TestSecurity(unittest.TestCase):
    def test_no_network_modules_imported_by_source(self):
        banned = ('socket', 'urllib', 'http', 'ftplib', 'smtplib', 'requests', 'ssl', 'telnetlib', 'xmlrpc', 'ctypes')
        for f in APP.glob('*.py*'):
            for n, line in enumerate(f.read_text(encoding='utf-8').splitlines(), start=1):
                s = line.strip()
                if s.startswith(('import ', 'from ')):
                    mods = s.replace(',', ' ').split()
                    for b in banned:
                        self.assertFalse(b in mods[1:2] or (s.startswith('import ') and b in mods[1:]),
                                         f'{f.name}:{n} importe {b}')

    def test_no_shell_true_or_eval(self):
        for f in APP.glob('*.py*'):
            src = f.read_text(encoding='utf-8')
            for bad in ('shell=True', 'eval(', 'exec(', 'os.system', 'pickle'):
                self.assertNotIn(bad, src, f'{f.name} contient {bad}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
