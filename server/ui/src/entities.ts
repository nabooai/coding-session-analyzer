import type { CorpusEntity } from "./api";

// Split a session's entities by the server's noise-floor verdict. served=0 -> the dim "one-off"
// fold; served=1 or ABSENT (older server, CSA_ENTITY_FLOOR=off) -> the main list, so pre-gate
// payloads render exactly as before. Order within each half is preserved (server sends ORDER BY id).
export function splitServed(entities: CorpusEntity[]): { served: CorpusEntity[]; oneOff: CorpusEntity[] } {
  const served: CorpusEntity[] = [];
  const oneOff: CorpusEntity[] = [];
  for (const e of entities) (e.served === 0 ? oneOff : served).push(e);
  return { served, oneOff };
}
