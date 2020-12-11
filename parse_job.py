#!/usr/bin/env python3
import json
import re
import shutil
import sys
from pathlib import Path
from zipfile import ZipFile

from calibre.ebooks.mobi.reader.mobi6 import MobiReader
from calibre.ebooks.mobi.reader.mobi8 import Mobi8Reader
from calibre.utils.config import config_dir
from calibre.utils.logging import default_log
from calibre_plugins.worddumb.database import (connect_ww_database,
                                               create_lang_layer, match_lemma)

NLTK_VERSION = '3.5'


def do_job(gui, books, plugin_path, abort, log, notifications):
    install_libs(plugin_path)
    ww_conn = connect_ww_database()

    for (_, book_fmt, asin, book_path, _) in books:
        ll_conn = create_lang_layer(asin, book_path)
        if ll_conn is None:
            continue

        for (start, word) in parse_book(book_path, book_fmt):
            match_lemma(start, word, ll_conn, ww_conn)

        ll_conn.commit()
        ll_conn.close()

    ww_conn.close()


def parse_book(path_of_book, book_fmt):
    if (book_fmt.lower() == 'kfx'):
        return parse_kfx(path_of_book)
    else:
        return parse_mobi(path_of_book, book_fmt)


def parse_kfx(path_of_book):
    from calibre_plugins.kfx_input.kfxlib import YJ_Book

    book = YJ_Book(path_of_book)
    data = book.convert_to_json_content()
    for entry in json.loads(data)['data']:
        for match_word in re.finditer('[a-zA-Z]{3,}', entry['content']):
            word = entry['content'][match_word.start():match_word.end()]
            yield (entry['position'] + match_word.start(), word)


def parse_mobi(pathtoebook, book_fmt):
    mobiReader = MobiReader(pathtoebook, default_log)
    html = b''
    offset = 1
    # use code from calibre.ebooks.mobi.reader.mobi8:Mobi8Reader.__call__
    if book_fmt.lower() == 'azw3' and mobiReader.kf8_type == 'joint':
        offset = mobiReader.kf8_boundary + 2
    mobiReader.extract_text(offset=offset)
    html = mobiReader.mobi_html
    if book_fmt.lower() == 'azw3':
        m8r = Mobi8Reader(mobiReader, default_log)
        m8r.kf8_sections = mobiReader.sections[offset-1:]
        m8r.read_indices()
        m8r.build_parts()
        html = b''.join(m8r.parts)

    # match text between HTML tags
    for match_text in re.finditer(b">[^<>]+<", html):
        text = html[match_text.start():match_text.end()]
        # match each word inside text
        for match_word in re.finditer(b"[a-zA-Z]{3,}", text):
            word = text[match_word.start():match_word.end()]
            start = match_text.start() + match_word.start()
            yield (start, word.decode('utf-8'))


def install_libs(plugin_path):
    extract_path = Path(config_dir).joinpath('plugins/worddumb-nltk'
                                             + NLTK_VERSION)
    if not extract_path.is_dir():
        for f in Path(config_dir).joinpath('plugins').iterdir():
            if 'worddumb' in f.name and f.is_dir():
                shutil.rmtree(f)  # delete old library folder

        with ZipFile(plugin_path, 'r') as zf:
            for f in zf.namelist():
                if '.venv' in f:
                    zf.extract(f, path=extract_path)

    for dir in extract_path.joinpath('.venv/lib').iterdir():
        sys.path.append(str(dir.joinpath('site-packages')))
    import nltk
    nltk.data.path.append(str(extract_path.joinpath('.venv/nltk_data')))
