// Docker HEALTHCHECK: exit 0 only when GET /healthz answers 200. No curl/wget needed in the image.
import http from 'node:http';

const port = Number.parseInt(process.env.PORT ?? '8085', 10);
const req = http.get({ host: '127.0.0.1', port, path: '/healthz', timeout: 4000 }, (res) => {
  res.resume();
  process.exit(res.statusCode === 200 ? 0 : 1);
});
req.on('timeout', () => {
  req.destroy();
  process.exit(1);
});
req.on('error', () => process.exit(1));
