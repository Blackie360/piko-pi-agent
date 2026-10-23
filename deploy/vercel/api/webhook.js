// Public receiver safety gate. No provider authentication contract has been verified.
// Do not parse, store, log or forward incoming messages until that prerequisite is met.
const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' };

export function POST() {
  return Response.json({
    accepted: false,
    error: 'provider_verification_not_configured',
    message: 'Piko is not accepting provider messages yet.'
  }, { status: 401, headers });
}

export function GET() {
  return Response.json({
    service: 'Piko WhatsApp receiver',
    active: false,
    status: 'blocked_pending_provider_verification'
  }, { status: 503, headers });
}

