# 4training-aquilla

Migration of the [4training.net](https://4training.net) translation system: from [mediawiki with Translate extension](https://www.mediawiki.org/wiki/Extension:Translate) to [Aquilla](https://aquilla.app). This repo contains [python scripts](scripts/) to turn the [4training.net](https://www.4training.net) worksheets into files that can be uploaded into the Aquilla importer, and it contains all these files.

- 29 worksheets with some dozens of translation units each (aquilla terminology: 29 files with some dozens of cells each)
- translated into more than 40 languages
- a total of around 450 translated worksheets (not all are translated in all languages)

English source lives in `mediawiki/` as MediaWiki wikitext. `scripts/clean_for_aquilla.py` writes:

- `aquilla/{Worksheet}.md` — English Markdown
- `aquilla/{lang}/{Worksheet}.csv` — bilingual CSV (`id`, `source`, `target`) for the Aquilla importer

Inline markup is limited to TipTap tags such as `<i>`, `<b>`, and `<br/>`.

Python 3.9+ is enough. There are no third-party dependencies. Both scripts call the [4training.net API](https://www.4training.net/api.php).

## Refresh the English sources

```sh
python3 scripts/fetch_mediawiki.py
```

Existing files are left in place. Pass `--force` to overwrite them, or name individual pages:

```sh
python3 scripts/fetch_mediawiki.py Forgiving_Step_by_Step Prayer
```

The page list matches [`ForTrainingLib.get_worksheet_list()`](https://github.com/4training/pywikitools/blob/main/pywikitools/fortraininglib.py).

## Build Markdown and CSVs

```sh
python3 scripts/clean_for_aquilla.py
```

That writes Markdown and, for every language in `LANGUAGE_LIST`, a CSV per worksheet. Limit the run with `-f md`, `-f csv`, or `-l de,fr`.

`LANGUAGE_LIST` is the set of languages that have at least one worksheet [resourcesbot](https://github.com/4training/pywikitools) lists for that language (a PDF on the same major version as the English original). English is the `source` column, so it is not a target language. `tr-tanri`, `ku-sinj`, and `uz-cyrl` are omitted, as are languages with nothing listed.

A CSV is written only when resourcesbot counts that worksheet as a translation: it appears in [`4training:{lang}.json`](https://www.4training.net/4training:De.json) because the title and version are translated and at least one unit is translated. Other worksheets for that language are skipped.

Worksheet text on 4training.net is [CC0](https://www.4training.net/).
