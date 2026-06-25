export default {
  async fetch(request) {
    const url = new URL(request.url);

    // Strip the /api prefix before forwarding to the backend.
    const backendPath = url.pathname.replace(/^\/api/, '') || '/';
    const backendUrl = new URL(`http://91.98.16.60:8000${backendPath}${url.search}`);

    const forwardedRequest = new Request(backendUrl, {
      method: request.method,
      headers: request.headers,
      body: request.method === 'GET' || request.method === 'HEAD'
          ? undefined
          : request.body,
      redirect: 'follow',
    });

    const response = await fetch(forwardedRequest);

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: response.headers,
    });
  },
};
