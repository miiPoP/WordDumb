#!/usr/bin/env python3

import re
import shutil
import zipfile
from collections import defaultdict
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote

try:
    from .mediawiki import (
        FUZZ_THRESHOLD,
        PERSON_LABELS,
        inception_text,
        is_full_name,
        query_mediawiki,
        query_wikidata,
        regime_type,
    )
    from .utils import CJK_LANGS
except ImportError:
    from mediawiki import (
        FUZZ_THRESHOLD,
        PERSON_LABELS,
        inception_text,
        is_full_name,
        query_mediawiki,
        query_wikidata,
        regime_type,
    )
    from utils import CJK_LANGS


NAMESPACES = {
    "n": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "ops": "http://www.idpf.org/2007/ops",
    "xml": "http://www.w3.org/1999/xhtml",
}


class EPUB:
    def __init__(
        self, book_path, mediawiki, wiki_commons, wikidata, custom_x_ray, lemma_glosses
    ):
        self.book_path = book_path
        self.mediawiki = mediawiki
        self.wiki_commons = wiki_commons
        self.wikidata = wikidata
        self.entity_id = 0
        self.entities = {}
        self.entity_occurrences = defaultdict(list)
        self.removed_entity_ids = set()
        self.extract_folder = Path(book_path).with_name("extract")
        if self.extract_folder.exists():
            shutil.rmtree(self.extract_folder)
        self.xhtml_folder = self.extract_folder
        self.xhtml_href_has_folder = False
        self.image_folder = self.extract_folder
        self.image_href_has_folder = False
        self.image_filenames = set()
        self.custom_x_ray = custom_x_ray
        self.lemma_glosses = lemma_glosses
        self.lemmas = {}
        self.lemma_id = 0

    def extract_epub(self):
        from lxml import etree

        with zipfile.ZipFile(self.book_path) as zf:
            zf.extractall(self.extract_folder)

        with self.extract_folder.joinpath("META-INF/container.xml").open("rb") as f:
            root = etree.fromstring(f.read())
            opf_path = root.find(".//n:rootfile", NAMESPACES).get("full-path")
            self.opf_path = self.extract_folder.joinpath(opf_path)
            if not self.opf_path.exists():
                self.opf_path = next(self.extract_folder.rglob(opf_path))
        with self.opf_path.open("rb") as opf:
            self.opf_root = etree.fromstring(opf.read())
            for item in self.opf_root.xpath(
                'opf:manifest/opf:item[starts-with(@media-type, "image/")]',
                namespaces=NAMESPACES,
            ):
                image = item.get("href")
                image_path = self.extract_folder.joinpath(image)
                if not image_path.exists():
                    image_path = next(self.extract_folder.rglob(image))
                if not image_path.parent.samefile(self.extract_folder):
                    self.image_folder = image_path.parent
                if "/" in image:
                    self.image_href_has_folder = True
                    break

            for item in self.opf_root.iterfind(
                'opf:manifest/opf:item[@media-type="application/xhtml+xml"]', NAMESPACES
            ):
                if item.get("properties") == "nav":
                    continue
                xhtml = item.get("href")
                xhtml_path = self.extract_folder.joinpath(xhtml)
                if not xhtml_path.exists():
                    xhtml_path = next(self.extract_folder.rglob(xhtml))
                if not xhtml_path.parent.samefile(self.extract_folder):
                    self.xhtml_folder = xhtml_path.parent
                if "/" in xhtml:
                    self.xhtml_href_has_folder = True
                with xhtml_path.open("r", encoding="utf-8") as f:
                    # remove soft hyphen
                    xhtml_text = re.sub(
                        r"\xad|&shy;|&#xAD;|&#xad;|&#173;", "", f.read()
                    )
                with xhtml_path.open("w", encoding="utf-8") as f:
                    f.write(xhtml_text)
                for match_body in re.finditer(
                    r"<body.{3,}?</body>", xhtml_text, re.DOTALL
                ):
                    for m in re.finditer(r">[^<]{2,}<", match_body.group(0)):
                        text = m.group(0)[1:-1].replace("\n", " ")
                        yield unescape(text), (
                            match_body.start() + m.start() + 1,
                            text,
                            xhtml_path,
                        )

    def add_entity(
        self, entity, ner_label, book_quote, start, end, xhtml_path, origin_entity
    ):
        from rapidfuzz.fuzz import token_set_ratio
        from rapidfuzz.process import extractOne

        if entity_data := self.entities.get(entity):
            entity_id = entity_data["id"]
            entity_data["count"] += 1
        elif entity not in self.custom_x_ray and (
            r := extractOne(
                entity,
                self.entities.keys(),
                score_cutoff=FUZZ_THRESHOLD,
                scorer=token_set_ratio,
            )
        ):
            matched_name = r[0]
            matched_entity = self.entities[matched_name]
            matched_entity["count"] += 1
            entity_id = matched_entity["id"]
            if is_full_name(matched_name, matched_entity["label"], entity, ner_label):
                self.entities[entity] = matched_entity
                del self.entities[matched_name]
        else:
            entity_id = self.entity_id
            self.entities[entity] = {
                "id": self.entity_id,
                "label": ner_label,
                "quote": book_quote,
                "count": 1,
            }
            self.entity_id += 1

        self.entity_occurrences[xhtml_path].append(
            (start, end, origin_entity, entity_id)
        )

    def add_lemma(self, lemma, start, end, xhtml_path, origin_text):
        self.entity_occurrences[xhtml_path].append((start, end, origin_text, lemma))
        if lemma not in self.lemmas:
            self.lemmas[lemma] = self.lemma_id
            self.lemma_id += 1

    def remove_entities(self, minimal_count):
        for entity, data in self.entities.copy().items():
            if (
                data["count"] < minimal_count
                and self.mediawiki.get_cache(entity) is None
                and entity not in self.custom_x_ray
            ):
                del self.entities[entity]
                self.removed_entity_ids.add(data["id"])

    def modify_epub(self, prefs, lang):
        if self.entities:
            query_mediawiki(self.entities, self.mediawiki, prefs["search_people"])
            if self.wikidata:
                query_wikidata(self.entities, self.mediawiki, self.wikidata)
            if prefs["minimal_x_ray_count"] > 1:
                self.remove_entities(prefs["minimal_x_ray_count"])
            self.create_x_ray_footnotes(prefs["search_people"], lang)
        self.insert_anchor_elements(lang)
        if self.lemmas:
            self.create_word_wise_footnotes(lang)
        self.modify_opf()
        self.zip_extract_folder()

    def insert_anchor_elements(self, lang):
        for xhtml_path, entity_list in self.entity_occurrences.items():
            if self.entities and self.lemmas:
                entity_list = sorted(entity_list, key=lambda x: x[0])

            with xhtml_path.open(encoding="utf-8") as f:
                xhtml_str = f.read()
            new_xhtml_str = ""
            last_end = 0
            for start, end, entity, entity_id in entity_list:
                if entity_id in self.removed_entity_ids:
                    continue
                new_xhtml_str += xhtml_str[last_end:start]
                if isinstance(entity_id, int):
                    new_xhtml_str += f'<a epub:type="noteref" href="x_ray.xhtml#{entity_id}">{entity}</a>'
                else:
                    new_xhtml_str += self.build_word_wise_tag(entity_id, entity, lang)
                last_end = end
            new_xhtml_str += xhtml_str[last_end:]

            # add epub namespace and Word Wise CSS
            with xhtml_path.open("w", encoding="utf-8") as f:
                if NAMESPACES["ops"] not in new_xhtml_str:
                    new_xhtml_str = new_xhtml_str.replace(
                        f'xmlns="{NAMESPACES["xml"]}"',
                        f'xmlns="{NAMESPACES["xml"]}" '
                        f'xmlns:epub="{NAMESPACES["ops"]}"',
                    )
                if self.lemmas:
                    new_xhtml_str = new_xhtml_str.replace(
                        "</head>",
                        "<style>body {line-height: 2.5;} ruby {text-decoration:overline;} ruby a {text-decoration:none;}</style></head>",
                    )
                f.write(new_xhtml_str)

    def build_word_wise_tag(self, word, origin_word, lang):
        short_def, *_ = self.get_lemma_gloss(word, lang)
        len_ratio = 5 if lang in CJK_LANGS else 2.5
        word_id = self.lemmas[word]
        if len(short_def) / len(word) > len_ratio:
            return f'<a epub:type="noteref" href="word_wise.xhtml#{word_id}">{origin_word}</a>'
        else:
            return f'<ruby><a epub:type="noteref" href="word_wise.xhtml#{word_id}">{origin_word}</a><rp>(</rp><rt>{short_def}</rt><rp>)</rp></ruby>'

    def create_x_ray_footnotes(self, search_people, lang):
        image_prefix = ""
        if self.xhtml_href_has_folder:
            image_prefix += "../"
        if self.image_href_has_folder:
            image_prefix += f"{self.image_folder.name}/"
        s = f"""
        <html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"
        lang="{lang}" xml:lang="{lang}">
        <head><title>X-Ray</title><meta charset="utf-8"/></head>
        <body>
        """
        for entity, data in self.entities.items():
            if custom_data := self.custom_x_ray.get(entity):
                custom_desc, custom_source, _ = custom_data
                if custom_desc:
                    s += f'<aside id="{data["id"]}" epub:type="footnote"><p>{escape(custom_desc)}</p>'
                    if source_data := self.mediawiki.get_source(custom_source):
                        source_name, source_link = source_data
                        if source_link:
                            s += f'<p>Source: <a href="{source_link}{quote(entity)}">{source_name}</a></p>'
                        else:
                            s += f"<p>Source: {source_name}</p>"
                    s += "</aside>"
                    continue

            if (search_people or data["label"] not in PERSON_LABELS) and (
                intro_cache := self.mediawiki.get_cache(entity)
            ):
                s += f"""
                <aside id="{data["id"]}" epub:type="footnote">
                <p>{escape(intro_cache["intro"])}</p>
                <p>Source: <a href="{self.mediawiki.source_link}{quote(entity)}">{self.mediawiki.source_name}</a></p>
                """
                if self.wikidata and (
                    wikidata_cache := self.wikidata.get_cache(intro_cache["item_id"])
                ):
                    if democracy_index := wikidata_cache.get("democracy_index"):
                        s += f"<p>{regime_type(float(democracy_index))}</p>"
                    if inception := wikidata_cache.get("inception"):
                        s += f"<p>{inception_text(inception)}</p>"
                    if self.wiki_commons and (
                        filename := wikidata_cache.get("map_filename")
                    ):
                        file_path = self.wiki_commons.get_image(filename)
                        s += f'<img style="max-width:100%" src="{image_prefix}{filename}" />'
                        shutil.copy(file_path, self.image_folder.joinpath(filename))
                        self.image_filenames.add(filename)
                    s += f'<p>Source: <a href="https://www.wikidata.org/wiki/{intro_cache["item_id"]}">Wikidata</a></p>'
                s += "</aside>"
            else:
                s += f'<aside id="{data["id"]}" epub:type="footnote"><p>{escape(data["quote"])}</p></aside>'

        s += "</body></html>"
        with self.xhtml_folder.joinpath("x_ray.xhtml").open("w", encoding="utf-8") as f:
            f.write(s)

        if self.wiki_commons:
            self.wiki_commons.close()

    def create_word_wise_footnotes(self, lang):
        s = f"""
        <html xmlns="http://www.w3.org/1999/xhtml"
        xmlns:epub="http://www.idpf.org/2007/ops"
        lang="{lang}" xml:lang="{lang}">
        <head><title>Word Wise</title><meta charset="utf-8"/></head>
        <body>
        """
        for lemma, lemma_id in self.lemmas.items():
            s += f'<aside id="{lemma_id}" epub:type="footnote">'
            _, gloss, example = self.get_lemma_gloss(lemma, lang)
            s += f"<p>{escape(gloss)}</p>"
            if example:
                s += f"<p><i>{escape(example)}</i></p>"
            s += f"<p>Source: <a href='https://en.wiktionary.org/wiki/{quote(lemma)}'>Wiktionary</a></p></aside>"

        s += "</body></html>"
        with self.xhtml_folder.joinpath("word_wise.xhtml").open(
            "w", encoding="utf-8"
        ) as f:
            f.write(s)

    def modify_opf(self):
        from lxml import etree

        xhtml_prefix = ""
        image_prefix = ""
        if self.xhtml_href_has_folder:
            xhtml_prefix = f"{self.xhtml_folder.name}/"
        if self.image_href_has_folder:
            image_prefix = f"{self.image_folder.name}/"
        manifest = self.opf_root.find("opf:manifest", NAMESPACES)
        if self.entities:
            s = f'<item href="{xhtml_prefix}x_ray.xhtml" id="x_ray.xhtml" media-type="application/xhtml+xml"/>'
            manifest.append(etree.fromstring(s))
        if self.lemmas:
            s = f'<item href="{xhtml_prefix}word_wise.xhtml" id="word_wise.xhtml" media-type="application/xhtml+xml"/>'
            manifest.append(etree.fromstring(s))
        for filename in self.image_filenames:
            filename_lower = filename.lower()
            if filename_lower.endswith(".svg"):
                media_type = "svg+xml"
            elif filename_lower.endswith(".png"):
                media_type = "png"
            elif filename_lower.endswith(".jpg"):
                media_type = "jpeg"
            elif filename_lower.endswith(".webp"):
                media_type = "webp"
            else:
                media_type = Path(filename).suffix.replace(".", "")
            s = f'<item href="{image_prefix}{filename}" id="{filename}" media-type="image/{media_type}"/>'
            manifest.append(etree.fromstring(s))
        spine = self.opf_root.find("opf:spine", NAMESPACES)
        if self.entities:
            spine.append(etree.fromstring('<itemref idref="x_ray.xhtml"/>'))
        if self.lemmas:
            spine.append(etree.fromstring('<itemref idref="word_wise.xhtml"/>'))
        with self.opf_path.open("w", encoding="utf-8") as f:
            f.write(etree.tostring(self.opf_root, encoding=str))

    def zip_extract_folder(self):
        self.book_path = Path(self.book_path)
        shutil.make_archive(self.extract_folder, "zip", self.extract_folder)
        new_filename = self.book_path.stem
        if self.entities:
            new_filename += "_x_ray"
        if self.lemmas:
            new_filename += "_word_wise"
        new_filename += ".epub"
        shutil.move(
            self.extract_folder.with_suffix(".zip"),
            self.book_path.with_name(new_filename),
        )
        shutil.rmtree(self.extract_folder)

    def get_lemma_gloss(self, lemma, lang):
        if lang in CJK_LANGS:  # pyahocorasick
            return self.lemma_glosses.get(lemma)[1:]
        else:  # flashtext
            return self.lemma_glosses.get_keyword(lemma)