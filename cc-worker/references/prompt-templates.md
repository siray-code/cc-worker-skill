# Dispatch examples

Copy the task shape, then replace paths, interfaces and commands with facts from
the target repository. Do not assume these example APIs exist. Codex investigates
and designs the task first; the worker implements it.

## Feature with tests

```json
{
  "id": "csv-export",
  "prompt": "Add exportUsersToCsv(filter: UserFilterDto): Promise<string> to the existing service using userRepo.findByFilter. Preserve current public methods. Return id,name,email,createdAt headers even for empty results; escape commas, quotes and newlines. Add cases for empty results, quoted fields and normal rows using existing test conventions. Do not change dependencies or introduce a new repository interface.",
  "read_files": ["src/types/user.ts", "src/services/base.ts"],
  "write_files": ["src/services/user.ts", "tests/user.test.ts"],
  "checks": [["npm", "run", "typecheck"], ["npm", "test", "--", "user.test.ts"]]
}
```

## Focused bug fix

```json
{
  "id": "token-expiry",
  "prompt": "Preserve verifyJwtToken's signature. Map TokenExpiredError to the existing AuthExpiredException and other verification failures to AuthInvalidException. Keep the current JWT library and error-message conventions. Add regression coverage showing callers can distinguish expiry from invalid signatures. Change no unrelated authentication behavior.",
  "read_files": ["src/errors/auth.error.ts"],
  "write_files": ["src/utils/token.ts", "src/utils/token.test.ts"],
  "checks": [["npx", "--no-install", "jest", "src/utils/token.test.ts", "--runInBand"]]
}
```

## Tests only

```json
{
  "id": "order-state-tests",
  "prompt": "Test transitionOrderState using the existing test framework. Cover PENDING to PAID, PAID to SHIPPED, and PENDING to CANCELLED. Assert IllegalStateTransitionError for SHIPPED to PENDING and CANCELLED to PAID. Do not change production code or introduce mocking dependencies.",
  "read_files": ["src/services/order-state.ts"],
  "write_files": ["tests/order-state.test.ts"],
  "checks": [["npm", "test", "--", "order-state.test.ts"]]
}
```

## Parallel grouping

Place independent tasks inside `{"tasks": [...]}` and use `batch --manifest`.
For example, a CSV service and an unrelated order-state test can run together if
their declared paths, interfaces and checks do not depend on each other. A shared
type update must be reviewed and integrated before dispatching tasks that consume it.
Batching does not resolve hidden semantic dependencies or shared external test state.

## Repair within the same session

Write a short correction file, then use `resume --run <run-id> --prompt-file <file>`:

```text
The runner check in <artifact-directory>/attempt-1/check-1.log failed because the
empty export does not include its header. Fix that behavior within the original
write_files boundaries and keep the existing escaping logic. The runner will rerun
the original checks. Stop and report if a required interface is missing.
```

Include only the evidence needed for the correction. If another worker already
modified a required input, review integration order instead of reusing a stale task.
