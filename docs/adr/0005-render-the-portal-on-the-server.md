# Render the Portal on the server

The Portal will initially render HTML on the home server and use focused client-side enhancement for interactive charts and controls, rather than maintaining a separately deployed single-page application. Browser routes and any narrow JSON endpoints will call transport-independent application services so a richer client can be added without replacing ingestion or domain logic. This trades some client-side freedom for one deployment, one authorization boundary, and fewer duplicated contracts in the single-Operator system.
