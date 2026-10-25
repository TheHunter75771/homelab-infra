// Test tool: shifts Date by a constant offset for the whole process (preload with --import). Used by
// time-travel.mjs to run the suite as if it were another day: tests that compare fixtures with the real clock
// would fail. Never loaded by the server in production. Offset in ms: ?offset=<n> on this module's URL.
const OFFSET = Number(new URL(import.meta.url).searchParams.get('offset') ?? 0);
if (Number.isFinite(OFFSET) && OFFSET !== 0) {
  const RealDate = globalThis.Date;
  class ShiftedDate extends RealDate {
    constructor(...args) {
      if (args.length === 0) super(RealDate.now() + OFFSET);
      else super(...args);
    }
    static now() {
      return RealDate.now() + OFFSET;
    }
  }
  globalThis.Date = ShiftedDate;
}
