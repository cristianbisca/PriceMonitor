import re

html = open('static/index.html', encoding='utf-8').read()
id_refs = set(re.findall(r"getElementById\('([^']+)'\)", html))
defined = set(re.findall(r'id="([^"]+)"', html))
missing = sorted(i for i in id_refs if i not in defined)
print('IDs referenced but not defined:', missing if missing else 'none')

onclicks = set(re.findall(r'onclick="(\w+)\(', html))
js_globals = set(re.findall(r'window\.(\w+)\s*=', html))
# functions declared with a plain `function name(` in global scope are also callable
plain_fns = set(re.findall(r'(?m)^\s{12}function (\w+)\(', html))
missing_fns = sorted(f for f in onclicks if f not in js_globals and f not in plain_fns)
print('onclick fns without window. binding:', missing_fns if missing_fns else 'none')

# the new candidate-related pieces must all be present
for token in ['candidatesSection', 'candidatesList', 'totalNewLinks',
              'window.approveCandidate', 'window.dismissCandidate',
              'function renderCandidates', 'function refreshDetailForProduct',
              'CANDIDATE_METHOD_LABELS', '/candidates']:
    assert token in html, f'missing: {token}'
print('candidate UI tokens: all present')
