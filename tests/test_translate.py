"""The PubMed-to-NeuroStore translator, ported verbatim from autonima with its tests."""

import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "translate", Path(__file__).resolve().parents[1] / "skills/neurostore/scripts/translate.py")
translate = importlib.util.module_from_spec(spec)
sys.modules["translate"] = translate
spec.loader.exec_module(translate)
translate_pubmed_query, QueryTranslationError = translate.translate_pubmed_query, translate.QueryTranslationError


def test_text_tags_are_removed_and_phrases_kept():
    t = translate_pubmed_query('"emotion regulation"[tiab] OR reappraisal[Title/Abstract]')
    assert t.query == '"emotion regulation" OR reappraisal'
    assert t.dropped == [] and t.approximated == []


def test_anded_mesh_is_dropped_but_ored_mesh_becomes_a_phrase():
    t = translate_pubmed_query(
        '(fMRI[tiab] OR "Magnetic Resonance Imaging, Functional"[mh]) AND Humans[mh]'
    )
    # NeuroStore matching ignores case; the heading keeps its own.
    assert t.query.lower() == 'fmri or "functional magnetic resonance imaging"'
    assert t.dropped == ["Humans[mh]"]
    assert t.approximated == ['"Magnetic Resonance Imaging, Functional"[mh]']


def test_publication_type_language_and_dates_are_dropped():
    t = translate_pubmed_query(
        '(reappraisal[tiab] AND ("2000/01/01"[Date - Publication] : "2019/12/31"[Date - Publication]))'
        ' AND English[lang] NOT (Review[pt] OR Comment[pt])'
    )
    assert t.query == "reappraisal"
    assert "English[lang]" in t.dropped and "Review[pt]" in t.dropped
    assert any("Date - Publication" in d for d in t.dropped)


def test_not_keeps_the_part_that_can_be_searched():
    t = translate_pubmed_query('PTSD[tiab] NOT (review[pt] OR "systematic review"[tiab])')
    assert t.query == 'PTSD NOT "systematic review"'


def test_truncation_becomes_word_endings():
    t = translate_pubmed_query("densit*[tiab]")
    assert t.query.startswith("densit OR ")
    assert "density" in t.query
    assert t.approximated == ["densit*[tiab]"]
    phrase = translate_pubmed_query('"drug cue*"[tiab]')
    assert '"drug cue"' in phrase.query and '"drug cuey"' in phrase.query


def test_untagged_words_bind_tighter_than_or():
    t = translate_pubmed_query("frontotemporal dementia OR bvFTD")
    assert t.query == "(frontotemporal dementia) OR bvFTD"


def test_tagged_words_run_together_are_a_phrase():
    assert translate_pubmed_query("cue reactivity[tiab]").query == '"cue reactivity"'


def test_booleans_evaluate_left_to_right_like_pubmed():
    # PubMed reads A OR B AND C as (A OR B) AND C; the output says so explicitly.
    assert translate_pubmed_query("a OR b AND c").query == "(a OR b) AND c"


def test_surplus_and_missing_parentheses_are_tolerated_and_reported():
    t = translate_pubmed_query("(fMRI[tiab] AND PTSD[tiab]))")
    assert t.query == "fMRI AND PTSD"
    assert any("unmatched ')'" in a for a in t.approximated)
    t = translate_pubmed_query("((fMRI[tiab] AND PTSD[tiab])")
    assert t.query == "fMRI AND PTSD"
    assert any("unclosed '('" in a for a in t.approximated)


def test_a_query_of_only_filters_cannot_be_translated():
    with pytest.raises(QueryTranslationError):
        translate_pubmed_query("Humans[mh] AND English[lang]")


def test_single_quoted_phrases():
    assert translate_pubmed_query("'social cognition'[Title/Abstract]").query == '"social cognition"'
