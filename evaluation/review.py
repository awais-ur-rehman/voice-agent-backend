"""Produce an escaped, local-only human review artifact from synthetic results."""

import argparse
from html import escape
import json
from pathlib import Path


def render_report(report: dict) -> str:
    sections = []
    for row in report.get('cases', []):
        passed = row.get('scores', {}).get('passed', False)
        payload = json.dumps(row, ensure_ascii=False, indent=2)
        response = row.get('response', {})
        reply = response.get('content', '') or 'Tool-only response; see tool arguments below.'
        tools = json.dumps(response.get('tools', []), ensure_ascii=False, indent=2)
        source = ('code template — no model request' if row.get('delivery_source') == 'code_template'
                  else 'model response')
        policy = ('<p>Action policy: ' + escape(str(row.get('action_policy', 'unrestricted')))
                  + '; eligible action count: ' + escape(str(row.get('eligible_action_count', 'not recorded')))
                  + '. State-constrained scores measure the gated pipeline, not unrestricted model accuracy.</p>'
                  if row.get('suite') == 'actions' else '')
        sections.append('<details' + ('' if passed else ' open') + '><summary>'
                        + escape(str(row['id'])) + ' — ' + ('mechanical checks pass; human review pending' if passed else 'needs review')
                        + '</summary><p>Delivery: ' + escape(source) + '</p>' + policy + '<p><strong>Caller</strong></p><p dir="auto">'
                        + escape(str(row.get('input', ''))) + '</p><p><strong>Model reply</strong></p><p dir="auto">'
                        + escape(reply) + '</p><pre dir="auto">' + escape(tools) + '</pre>'
                        + '<details><summary>Context, expected result, and mechanical checks</summary>'
                        + '<pre dir="auto">' + escape(payload) + '</pre></details></details>')
    return ('<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Phase 0 human review</title><style>body{font:16px/1.7 system-ui;max-width:1000px;'
            'margin:40px auto;padding:0 20px;color:#222}summary{cursor:pointer;padding:12px 0}'
            'details{border-top:1px solid #ccc}pre{white-space:pre-wrap;overflow-wrap:anywhere;'
            'font:15px/2 system-ui;background:#f6f6f6;padding:16px}</style>'
            '<h1>Phase 0 human review</h1><p>Human review pending. Synthetic data only. '
            'Automated checks do not prove Urdu naturalness, clinical safety, or working scheduling.</p>'
            '<p>Model: ' + escape(str(report.get('model', 'unknown'))) + '</p>'
            + ''.join(sections) + '</html>')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('report', type=Path)
    parser.add_argument('--suite', choices=['all', 'actions', 'language'], default='all')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1] / 'results'
    source = args.report.resolve()
    if not source.is_relative_to(root.resolve()):
        parser.error('Read a synthetic report inside backend/results/')
    destination = source.with_name(source.stem + ('' if args.suite == 'all' else '-' + args.suite) + '.html')
    if destination.exists():
        parser.error('Review HTML already exists; do not overwrite a reviewed artifact')
    try:
        report = json.loads(source.read_text())
        if args.suite != 'all':
            report['cases'] = [row for row in report.get('cases', []) if row.get('suite') == args.suite]
            if not report['cases']:
                parser.error('The selected report contains no cases in that suite')
        document = render_report(report)
        with destination.open('x') as handle:
            destination.chmod(0o600)
            handle.write(document)
    except (OSError, ValueError, KeyError):
        parser.error('Could not read the report or create review HTML')
    print(destination)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
