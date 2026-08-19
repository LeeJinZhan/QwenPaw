# Bank Runtime Console PawApp

This read-only PawApp is an optional local/dev diagnostic surface. It is not installed in the production Bank Runtime Worker image.

Required server-side environment variables:

- `QWENPAW_RUNTIME_CONSOLE_BASE_URL`
- `QWENPAW_RUNTIME_CONSOLE_APP_ID`
- `QWENPAW_RUNTIME_CONSOLE_APP_TOKEN`
- `QWENPAW_RUNTIME_CONSOLE_APP_SCOPES` (defaults to `assistant:read`)

The browser supplies only external `user_id` and `org_id` context. Runtime application credentials remain server-side. The app exposes connect, list, and detail reads only; disconnect clears this browser tab's `sessionStorage` state.
