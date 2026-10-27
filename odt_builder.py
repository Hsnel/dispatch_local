# -*- coding: utf-8 -*-
"""Création du document ODT d'un bordereau d'envoi.

Bibliothèque standard uniquement. Aucun accès réseau.
Le document produit s'ouvre et se modifie normalement dans LibreOffice Writer.
Les données saisies sont aussi conservées dans les propriétés du fichier
(meta.xml) pour pouvoir reconstruire l'index si la base est perdue.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import struct
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

MIME = 'application/vnd.oasis.opendocument.text'
TIMES = 'Times New Roman'
COMIC = 'Comic Sans MS'
USABLE_WIDTH_CM = 17.0          # A4 moins 2 x 2 cm de marge
COLS_CM = (9.70, 1.59, 5.71)    # DESIGNATION / Nbr / OBSERVATION = 17 cm
JSON_PROP = 'bordereau_json'

NS = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
    'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
    'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0" '
    'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"'
)
FONT_DECLS = (
    '<office:font-face-decls>'
    f'<style:font-face style:name="{TIMES}" svg:font-family="\'{TIMES}\'" style:font-family-generic="roman"/>'
    f'<style:font-face style:name="{COMIC}" svg:font-family="\'{COMIC}\'" style:font-family-generic="script"/>'
    '</office:font-face-decls>'
)

_CTRL = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]')


def clean(s) -> str:
    """Retire les caractères de contrôle interdits en XML."""
    return _CTRL.sub('', str(s))


def txt(s) -> str:
    """Texte sûr pour ODF : échappe le XML, conserve espaces multiples, tabulations, retours à la ligne."""
    s = clean(s).replace('\r\n', '\n').replace('\r', '\n')
    out = []
    for i, line in enumerate(s.split('\n')):
        if i:
            out.append('<text:line-break/>')
        line = escape(line).replace('\t', '<text:tab/>')
        line = re.sub(r'^ ', '<text:s/>', line)
        line = re.sub(r' {2,}', lambda m: ' <text:s text:c="%d"/>' % (len(m.group()) - 1), line)
        out.append(line)
    return ''.join(out)


def fr_date(iso: str) -> str:
    y, m, d = iso.split('-')
    return f'{d}/{m}/{y}'


# ---------------------------------------------------------------- images
def image_info(data: bytes):
    """Retourne (largeur_px, hauteur_px, extension, type_mime) pour un PNG ou un JPEG."""
    if data[:8] == b'\x89PNG\r\n\x1a\n' and len(data) >= 24:
        w, h = struct.unpack('>II', data[16:24])
        return w, h, 'png', 'image/png'
    if data[:2] == b'\xff\xd8':
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack('>H', data[i + 2:i + 4])[0]
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                h, w = struct.unpack('>HH', data[i + 5:i + 9])
                return w, h, 'jpg', 'image/jpeg'
            i += 2 + seg
    raise ValueError("Le logo doit être une image PNG ou JPEG valide.")


def load_logo(path, max_height_cm=3.2):
    if not path:
        return None
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"Le logo indiqué dans config.ini est introuvable : {p}")
    data = p.read_bytes()
    if len(data) > 8_000_000:
        raise ValueError("Le logo est trop volumineux (8 Mo maximum).")
    w, h, ext, mime = image_info(data)
    if w <= 0 or h <= 0:
        raise ValueError("Le logo n'a pas de dimensions valides.")
    width_cm = USABLE_WIDTH_CM
    height_cm = width_cm * h / w
    if height_cm > max_height_cm:    # on limite la hauteur de l'en-tête
        height_cm = max_height_cm
        width_cm = height_cm * w / h
    return {'data': data, 'name': f'logo.{ext}', 'mime': mime, 'w': width_cm, 'h': height_cm}


# ---------------------------------------------------------------- styles
def _ptext(font=TIMES, size=12, bold=False, italic=False, underline=False):
    a = [f'style:font-name="{font}"', f'fo:font-size="{size}pt"']
    if bold:
        a.append('fo:font-weight="bold"')
    if italic:
        a.append('fo:font-style="italic"')
    if underline:
        a.append('style:text-underline-style="solid" style:text-underline-width="auto" '
                 'style:text-underline-color="font-color"')
    return '<style:text-properties ' + ' '.join(a) + '/>'


def _pstyle(name, align=None, mt=0.0, mb=0.0, **text):
    p = [f'fo:margin-top="{mt}cm"', f'fo:margin-bottom="{mb}cm"']
    if align:
        p.append(f'fo:text-align="{align}"')
    return (f'<style:style style:name="{name}" style:family="paragraph" style:parent-style-name="Standard">'
            f'<style:paragraph-properties {" ".join(p)}/>{_ptext(**text)}</style:style>')


def _list_style(name, left_cm, relative_size=None):
    rel = f' text:bullet-relative-size="{relative_size}%"' if relative_size else ''
    return (f'<text:list-style style:name="{name}">'
            f'<text:list-level-style-bullet text:level="1" text:bullet-char="•"{rel}>'
            '<style:list-level-properties text:list-level-position-and-space-mode="label-alignment">'
            f'<style:list-level-label-alignment text:label-followed-by="listtab" '
            f'text:list-tab-stop-position="{left_cm}cm" fo:text-indent="-0.55cm" fo:margin-left="{left_cm}cm"/>'
            '</style:list-level-properties>'
            f'<style:text-properties style:font-name="{TIMES}"/>'
            '</text:list-level-style-bullet></text:list-style>')


def styles_xml(logo, footer_lines) -> str:
    named = ''.join([
        '<style:style style:name="Standard" style:family="paragraph" style:class="text"/>',
        _pstyle('Bdx_Ref', size=10, bold=True),
        _pstyle('Bdx_Espace', mb=0.5),
        _pstyle('Bdx_A', 'center', mb=0.2, font=COMIC, size=16, bold=True),
        _pstyle('Bdx_Dest', 'center', mb=0.1, font=COMIC, size=14, bold=True),
        _pstyle('Bdx_Titre', 'center', mb=0.5, font=COMIC, size=14, bold=True, italic=True, underline=True),
        _pstyle('Bdx_ThCol', 'center', size=12, bold=True, italic=True),
        _pstyle('Bdx_TitreLigne', 'center', mt=0.15, mb=0.25, size=12, bold=True, underline=True),
        _pstyle('Bdx_Ligne', mt=0.07, mb=0.07, size=12, bold=True),
        _pstyle('Bdx_Nb', 'center', mt=0.07, mb=0.07, size=12, bold=True),
        _pstyle('Bdx_Obs', 'center', mt=0.07, mb=0.07, size=12, bold=True, italic=True),
        _pstyle('Bdx_Concernant', 'center', mt=0.25, mb=0.15, size=12, bold=True, underline=True),
        _pstyle('Bdx_Detail', mt=0.07, mb=0.07, size=12, bold=True, italic=True),
        _pstyle('Bdx_Fonction', 'center', mt=0.4, mb=0.15, size=12, bold=True),
        _pstyle('Bdx_Vide', size=12),
        _pstyle('Bdx_Entete', 'center', size=12),
        _pstyle('Bdx_Pied', 'center', size=9),
        '<style:style style:name="Bdx_PiedPremier" style:family="paragraph" style:parent-style-name="Bdx_Pied">'
        '<style:paragraph-properties fo:border-top="0.5pt solid #999999" fo:padding-top="0.15cm"/></style:style>',
        _list_style('Puce', 0.9),
        _list_style('PuceFine', 1.6, relative_size=55),
    ])
    header = ''
    if logo:
        header = (
            '<style:header><text:p text:style-name="Bdx_Entete">'
            f'<draw:frame draw:style-name="frLogo" draw:name="Logo" text:anchor-type="as-char" '
            f'svg:width="{logo["w"]:.2f}cm" svg:height="{logo["h"]:.2f}cm" draw:z-index="0">'
            f'<draw:image xlink:href="Pictures/{logo["name"]}" xlink:type="simple" xlink:show="embed" '
            'xlink:actuate="onLoad"/></draw:frame></text:p></style:header>')
    footer = ''
    if footer_lines:
        paras = ''.join(
            f'<text:p text:style-name="{"Bdx_PiedPremier" if i == 0 else "Bdx_Pied"}">{txt(line)}</text:p>'
            for i, line in enumerate(footer_lines))
        footer = f'<style:footer>{paras}</style:footer>'
    top = 1.0 if logo else 2.0
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-styles {NS} office:version="1.3">'
        f'{FONT_DECLS}'
        '<office:styles>'
        '<style:default-style style:family="paragraph">'
        '<style:paragraph-properties fo:orphans="2" fo:widows="2"/>'
        f'<style:text-properties style:font-name="{TIMES}" fo:font-size="12pt" fo:language="fr" fo:country="FR"/>'
        '</style:default-style>'
        f'{named}'
        '</office:styles>'
        '<office:automatic-styles>'
        '<style:page-layout style:name="pm1">'
        '<style:page-layout-properties fo:page-width="21cm" fo:page-height="29.7cm" style:print-orientation="portrait" '
        f'fo:margin-top="{top}cm" fo:margin-bottom="1.2cm" fo:margin-left="2cm" fo:margin-right="2cm"/>'
        '<style:header-style><style:header-footer-properties fo:min-height="0cm" fo:margin-bottom="0.4cm"/></style:header-style>'
        '<style:footer-style><style:header-footer-properties fo:min-height="0cm" fo:margin-top="0.4cm"/></style:footer-style>'
        '</style:page-layout>'
        '<style:style style:name="frLogo" style:family="graphic">'
        '<style:graphic-properties style:vertical-pos="top" style:vertical-rel="baseline" style:wrap="none" '
        'fo:margin-top="0cm" fo:margin-bottom="0cm" fo:margin-left="0cm" fo:margin-right="0cm" fo:padding="0cm" fo:border="none"/>'
        '</style:style>'
        '</office:automatic-styles>'
        f'<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="pm1">{header}{footer}'
        '</style:master-page></office:master-styles>'
        '</office:document-styles>')


# ---------------------------------------------------------------- contenu
def _auto_styles() -> str:
    b = 'fo:padding-left="0.21cm" fo:padding-right="0.21cm"'
    line = '0.5pt solid #000000'
    return (
        '<office:automatic-styles>'
        '<style:style style:name="Tb" style:family="table">'
        f'<style:table-properties style:width="{USABLE_WIDTH_CM}cm" table:align="margins" table:border-model="collapsing"/></style:style>'
        + ''.join(f'<style:style style:name="TbC{i + 1}" style:family="table-column">'
                  f'<style:table-column-properties style:column-width="{w}cm"/></style:style>'
                  for i, w in enumerate(COLS_CM))
        + '<style:style style:name="TrH" style:family="table-row"><style:table-row-properties style:min-row-height="1.2cm" fo:keep-together="always"/></style:style>'
        '<style:style style:name="TrB" style:family="table-row"><style:table-row-properties fo:keep-together="always"/></style:style>'
        '<style:style style:name="TrE" style:family="table-row"><style:table-row-properties style:min-row-height="1.8cm" fo:keep-together="always"/></style:style>'
        f'<style:style style:name="CeH" style:family="table-cell"><style:table-cell-properties {b} fo:padding-top="0.1cm" fo:padding-bottom="0.1cm" '
        f'fo:border="{line}" fo:background-color="#f2f2f2" style:vertical-align="middle"/></style:style>'
        f'<style:style style:name="CeB" style:family="table-cell"><style:table-cell-properties {b} fo:padding-top="0.03cm" fo:padding-bottom="0.03cm" '
        f'fo:border-left="{line}" fo:border-right="{line}" fo:border-top="none" fo:border-bottom="none"/></style:style>'
        f'<style:style style:name="CeBF" style:family="table-cell"><style:table-cell-properties {b} fo:padding-top="0.03cm" fo:padding-bottom="0.03cm" '
        f'fo:border-left="{line}" fo:border-right="{line}" fo:border-top="{line}" fo:border-bottom="none"/></style:style>'
        f'<style:style style:name="CeE" style:family="table-cell"><style:table-cell-properties {b} fo:padding-top="0.03cm" fo:padding-bottom="0.03cm" '
        f'fo:border-left="{line}" fo:border-right="{line}" fo:border-top="none" fo:border-bottom="{line}"/></style:style>'
        '<style:style style:name="Tu" style:family="text"><style:text-properties style:text-underline-style="solid" '
        'style:text-underline-width="auto" style:text-underline-color="font-color"/></style:style>'
        '</office:automatic-styles>')


_VIDE = '<text:p text:style-name="Bdx_Vide"/>'


def _cell(style, inner):
    return f'<table:table-cell table:style-name="{style}" office:value-type="string">{inner}</table:table-cell>'


def _bullet(style_list, style_par, text):
    return (f'<text:list text:style-name="{style_list}"><text:list-item>'
            f'<text:p text:style-name="{style_par}">{txt(text)}</text:p></text:list-item></text:list>')


def _blank_lines(doc_cfg) -> int:
    try:
        return min(max(int(str(doc_cfg.get('lignes_vides_avant_date', '0')).strip() or 0), 0), 10)
    except ValueError:
        return 0


def content_xml(d: dict, doc_cfg: dict) -> str:
    # n° officiel attribué par le bureau d'ordre : absent à l'impression => pointillés à compléter à la main
    ref = doc_cfg.get('reference', 'N° {numero}').replace('{numero}', str(d['officiel']) if d.get('officiel') else '.' * 10)
    col_nb = doc_cfg.get('colonne_nombre', 'Nbr') or 'Nbr'

    # lignes du tableau : (cellule désignation, cellule nombre, cellule observation)
    rows = []
    obs = f'« {d["observation"]} »' if d.get('observation') else ''
    obs_xml = f'<text:p text:style-name="Bdx_Obs">{txt(obs)}</text:p>' if obs else _VIDE
    obs_used = False

    def next_obs():
        nonlocal obs_used
        if obs_used:
            return _VIDE
        obs_used = True
        return obs_xml

    rows.append((f'<text:p text:style-name="Bdx_TitreLigne">{txt(d["titre"])}</text:p>', _VIDE,
                 _VIDE if d['lignes'] else next_obs()))
    for des, nb in d['lignes']:
        rows.append((_bullet('Puce', 'Bdx_Ligne', des),
                     f'<text:p text:style-name="Bdx_Nb">{txt(nb)}</text:p>' if nb else _VIDE,
                     next_obs()))
    conc = [x for x in d.get('concernant', '').split('\n') if x.strip()]
    if conc:
        rows.append((f'<text:p text:style-name="Bdx_Concernant">Concernant</text:p>', _VIDE, _VIDE))
        for line in conc:
            rows.append((_bullet('PuceFine', 'Bdx_Detail', line), _VIDE, _VIDE))
    if d.get('en_fonction'):
        rows.append((f'<text:p text:style-name="Bdx_Fonction"><text:span text:style-name="Tu">En fonction à</text:span>'
                     f' : {txt(d["en_fonction"])}</text:p>', _VIDE, _VIDE))

    trs = ['<table:table-header-rows><table:table-row table:style-name="TrH">'
           + _cell('CeH', f'<text:p text:style-name="Bdx_ThCol">DESIGNATION</text:p>')
           + _cell('CeH', f'<text:p text:style-name="Bdx_ThCol">{txt(col_nb)}</text:p>')
           + _cell('CeH', '<text:p text:style-name="Bdx_ThCol">OBSERVATION</text:p>')
           + '</table:table-row></table:table-header-rows>']
    for i, (a, b, c) in enumerate(rows):
        st = 'CeBF' if i == 0 else 'CeB'
        trs.append(f'<table:table-row table:style-name="TrB">{_cell(st, a)}{_cell(st, b)}{_cell(st, c)}</table:table-row>')
    trs.append('<table:table-row table:style-name="TrE">' + _cell('CeE', _VIDE) * 3 + '</table:table-row>')

    table = ('<table:table table:name="Bordereau" table:style-name="Tb">'
             + ''.join(f'<table:table-column table:style-name="TbC{i + 1}"/>' for i in range(3))
             + ''.join(trs) + '</table:table>')

    body = (
        f'<text:p text:style-name="Bdx_Ref">{txt(ref)}</text:p>'
        f'<text:p text:style-name="Bdx_Ref">{txt(doc_cfg.get("signataire", "Le Directeur"))}</text:p>'
        + '<text:p text:style-name="Bdx_Ref"/>' * _blank_lines(doc_cfg) +
        f'<text:p text:style-name="Bdx_Ref">{fr_date(d["date"])}</text:p>'
        '<text:p text:style-name="Bdx_Espace"/>'
        '<text:p text:style-name="Bdx_A">A</text:p>'
        f'<text:p text:style-name="Bdx_Dest">{txt(d["destinataire"])}</text:p>'
        '<text:p text:style-name="Bdx_Titre">Bordereau d\'envoi</text:p>'
        + table)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-content {NS} office:version="1.3">{FONT_DECLS}{_auto_styles()}'
            f'<office:body><office:text>{body}</office:text></office:body></office:document-content>')


def meta_xml(d: dict) -> str:
    payload = {k: d[k] for k in ('numero', 'officiel', 'annee', 'date', 'destinataire', 'titre', 'observation',
                                 'lignes', 'concernant', 'en_fonction')}
    payload['version'] = 2      # v1 : « numero » était le n° officiel ; v2 : « numero » = référence interne
    js = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    now = dt.datetime.now().strftime('%Y-%m-%dT%H:%M:%S')
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            f'<office:document-meta {NS} office:version="1.3"><office:meta>'
            '<meta:generator>Bordereaux/1.0</meta:generator>'
            f'<dc:title>{txt("Bordereau " + str(d["annee"]) + "-" + format(int(d["numero"]), "04d"))}</dc:title>'
            f'<meta:creation-date>{now}</meta:creation-date>'
            f'<meta:user-defined meta:name="{JSON_PROP}">{escape(clean(js))}</meta:user-defined>'
            '</office:meta></office:document-meta>')


def manifest_xml(logo) -> str:
    entries = [('/', MIME), ('content.xml', 'text/xml'), ('styles.xml', 'text/xml'), ('meta.xml', 'text/xml')]
    if logo:
        entries.append((f'Pictures/{logo["name"]}', logo['mime']))
    items = ''.join(
        f'<manifest:file-entry manifest:full-path="{p}" manifest:media-type="{m}"'
        + (' manifest:version="1.3"' if p == '/' else '') + '/>' for p, m in entries)
    return ('<?xml version="1.0" encoding="UTF-8"?>'
            '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" '
            f'manifest:version="1.3">{items}</manifest:manifest>')


def build_odt(d: dict, doc_cfg: dict, out_path) -> None:
    """Écrit le document ODT du bordereau `d` (données déjà validées) dans `out_path`."""
    try:
        max_h = min(max(float(str(doc_cfg.get('logo_hauteur_max', '6')).replace(',', '.')), 1.0), 12.0)
    except ValueError:
        max_h = 6.0
    logo = load_logo(doc_cfg.get('logo', ''), max_h)
    footer = [doc_cfg.get(k, '').strip() for k in ('pied_ligne1', 'pied_ligne2', 'pied_ligne3')]
    footer = [x for x in footer if x]
    with zipfile.ZipFile(out_path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), MIME, compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/manifest.xml', manifest_xml(logo), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('content.xml', content_xml(d, doc_cfg), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('styles.xml', styles_xml(logo, footer), compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('meta.xml', meta_xml(d), compress_type=zipfile.ZIP_DEFLATED)
        if logo:
            z.writestr(f'Pictures/{logo["name"]}', logo['data'], compress_type=zipfile.ZIP_STORED)


def read_data(path):
    """Relit les données d'un bordereau depuis son fichier ODT (None si absentes)."""
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo('meta.xml')
            if info.file_size > 5_000_000:
                return None
            meta = z.read('meta.xml').decode('utf-8', 'replace')
    except (OSError, KeyError, zipfile.BadZipFile):
        return None
    m = re.search(r'<meta:user-defined[^>]*meta:name="%s"[^>]*>(.*?)</meta:user-defined>' % JSON_PROP, meta, re.S)
    if not m:
        return None
    raw = (m.group(1).replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
           .replace('&apos;', "'").replace('&amp;', '&'))
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------- liste exportée (tableur .ods)
_ODS_NS = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
           'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
           'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
           'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
           'xmlns:fo="urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"')


def build_ods(header, rows, widths_cm, out_path, sheet='Bordereaux') -> None:
    """Tableur LibreOffice minimal : une ligne d'en-têtes (grisée, en gras) puis les lignes.
    Toutes les cellules sont du TEXTE : rien n'est interprété comme une formule ou un nombre (0042 reste 0042)."""
    cols = ''.join(f'<table:table-column table:style-name="co{i}"/>' for i in range(len(header)))
    colstyles = ''.join(
        f'<style:style style:name="co{i}" style:family="table-column">'
        f'<style:table-column-properties style:column-width="{w:.2f}cm"/></style:style>'
        for i, w in enumerate(widths_cm))

    def cell(v, style):
        return (f'<table:table-cell table:style-name="{style}" office:value-type="string">'
                f'<text:p>{txt(v)}</text:p></table:table-cell>')
    body = ('<table:table-header-rows><table:table-row>' + ''.join(cell(h, 'ceH') for h in header)
            + '</table:table-row></table:table-header-rows>')      # l'en-tête se répète à chaque page imprimée
    for r in rows:
        body += '<table:table-row>' + ''.join(cell(v, 'ceB') for v in r) + '</table:table-row>'
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-content {_ODS_NS} office:version="1.3"><office:automatic-styles>{colstyles}'
        '<style:style style:name="ta1" style:family="table" style:master-page-name="Standard">'
        '<style:table-properties table:display="true"/></style:style>'
        '<style:style style:name="ceH" style:family="table-cell"><style:table-cell-properties '
        'fo:background-color="#e5e7eb" fo:border="0.5pt solid #999999" fo:wrap-option="wrap"/>'
        '<style:text-properties fo:font-weight="bold"/></style:style>'
        '<style:style style:name="ceB" style:family="table-cell"><style:table-cell-properties '
        'fo:border="0.5pt solid #cccccc" fo:wrap-option="wrap"/></style:style>'
        f'</office:automatic-styles><office:body><office:spreadsheet><table:table table:name="{txt(sheet)}" table:style-name="ta1">'
        f'{cols}{body}</table:table></office:spreadsheet></office:body></office:document-content>')
    styles = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<office:document-styles {_ODS_NS} office:version="1.3"><office:automatic-styles>'
        '<style:page-layout style:name="pm1"><style:page-layout-properties fo:page-width="29.7cm" '
        'fo:page-height="21cm" style:print-orientation="landscape" fo:margin-top="1.5cm" fo:margin-bottom="1.5cm" '
        'fo:margin-left="1.5cm" fo:margin-right="1.5cm"/></style:page-layout></office:automatic-styles>'
        '<office:master-styles><style:master-page style:name="Standard" style:page-layout-name="pm1"/>'
        '</office:master-styles></office:document-styles>')
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.3">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        '<manifest:file-entry manifest:full-path="styles.xml" manifest:media-type="text/xml"/>'
        '</manifest:manifest>')
    with zipfile.ZipFile(out_path, 'w') as z:
        z.writestr(zipfile.ZipInfo('mimetype'), 'application/vnd.oasis.opendocument.spreadsheet',
                   compress_type=zipfile.ZIP_STORED)
        z.writestr('META-INF/manifest.xml', manifest, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('content.xml', content, compress_type=zipfile.ZIP_DEFLATED)
        z.writestr('styles.xml', styles, compress_type=zipfile.ZIP_DEFLATED)
