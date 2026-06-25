## Cloudflare API Proxy Setup

Use this when the website runs on `https://conscout.com` but the backend still runs on `http://91.98.16.60:8000`.

### 1. Backend

Make sure `.env` contains:

```env
ALLOWED_ORIGINS=https://conscout.com,https://www.conscout.com,https://conscout-web.pages.dev
```

Then restart the backend service.

### 2. Create Worker

In Cloudflare:

1. Go to `Workers & Pages`
2. Create a new `Worker`
3. Replace the default code with the contents of:

`cloudflare_api_proxy_worker.js`

4. Deploy the Worker

### 3. Add Route

Add this route to the Worker:

```text
conscout.com/api/*
```

If you also use `www`, add:

```text
www.conscout.com/api/*
```

### 4. Result

These browser calls:

```text
https://conscout.com/api/auth/login
https://conscout.com/api/projects
https://conscout.com/api/subscriptions/request
```

will be forwarded to:

```text
http://91.98.16.60:8000/auth/login
http://91.98.16.60:8000/projects
http://91.98.16.60:8000/subscriptions/request
```

### 5. Test

Open:

```text
https://conscout.com/api/health
```

It should return a healthy backend response.
