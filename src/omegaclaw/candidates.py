"""Research suggestions and their explicit mathematical specifications."""
from .contracts import ContractError
from .specification import validate_spec


def formalization_spec(candidate, *, allow_legacy=False):
    kind = candidate.get('candidate_kind')
    if kind is None and allow_legacy:
        return None
    if kind not in {'mathematical-claim', 'research-move'}:
        raise ContractError('candidate_kind must distinguish mathematical-claim from research-move')
    if kind == 'research-move':
        if candidate.get('formalization_spec') is not None:
            raise ContractError('a research move cannot supply a mathematical formalization specification')
        return None
    try:
        spec = validate_spec(candidate.get('formalization_spec'))
    except ValueError as exc:
        raise ContractError('mathematical candidate requires a complete formalization_spec: ' + str(exc)) from exc
    if spec['theorem'] != candidate['statement'].strip() or spec['targets'] != [spec['theorem']]:
        raise ContractError('candidate specification must bind its exact statement as its sole target')
    return spec
