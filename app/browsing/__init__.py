"""The agents' window on the public web: search, news, page fetching and scraping, and an
interactive headless browser. Every connection goes through netguard, so nothing here can
reach the server's own services (Postgres, MinIO, Grafana, vLLM) or the private network."""
