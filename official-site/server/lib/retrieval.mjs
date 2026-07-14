const HAN = /\p{Script=Han}/u;
const WORD = /[\p{Letter}\p{Number}]/u;

export function tokenize(value) {
  const normalized = String(value || '').normalize('NFKC').toLowerCase();
  const tokens = [];
  let latin = '';
  let hanRun = '';

  const flushLatin = () => {
    if (latin.length > 1) tokens.push(latin);
    latin = '';
  };
  const flushHan = () => {
    if (!hanRun) return;
    for (const char of hanRun) tokens.push(char);
    for (let index = 0; index < hanRun.length - 1; index += 1) {
      tokens.push(hanRun.slice(index, index + 2));
    }
    hanRun = '';
  };

  for (const char of normalized) {
    if (HAN.test(char)) {
      flushLatin();
      hanRun += char;
    } else if (WORD.test(char)) {
      flushHan();
      latin += char;
    } else {
      flushLatin();
      flushHan();
    }
  }
  flushLatin();
  flushHan();
  return tokens;
}
export function createRetriever(corpus) {
  const chunks = Array.isArray(corpus?.chunks) ? corpus.chunks : [];
  const documents = chunks.map((chunk) => {
    const terms = tokenize(`${chunk.title} ${chunk.title} ${chunk.text}`);
    const frequencies = new Map();
    for (const term of terms) frequencies.set(term, (frequencies.get(term) || 0) + 1);
    return { ...chunk, terms, frequencies };
  });
  const averageLength = documents.length
    ? documents.reduce((total, document) => total + document.terms.length, 0) / documents.length
    : 1;
  const documentFrequency = new Map();
  for (const document of documents) {
    for (const term of new Set(document.terms)) {
      documentFrequency.set(term, (documentFrequency.get(term) || 0) + 1);
    }
  }

  return function search(query, limit = 4) {
    const queryTerms = tokenize(query);
    if (!documents.length || !queryTerms.length) return [];
    const uniqueTerms = [...new Set(queryTerms)];
    const k1 = 1.4;
    const b = 0.72;
    const scored = documents.map((document) => {
      let score = 0;
      for (const term of uniqueTerms) {
        const frequency = document.frequencies.get(term) || 0;
        if (!frequency) continue;
        const df = documentFrequency.get(term) || 0;
        const idf = Math.log(1 + (documents.length - df + 0.5) / (df + 0.5));
        const denominator = frequency + k1 * (1 - b + b * document.terms.length / averageLength);
        score += idf * ((frequency * (k1 + 1)) / denominator);
      }
      return { id: document.id, title: document.title, text: document.text, score };
    });

    return scored
      .sort((left, right) => right.score - left.score || left.id.localeCompare(right.id))
      .slice(0, Math.max(1, limit));
  };
}
