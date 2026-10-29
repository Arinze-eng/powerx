import { assert, assertEquals, assertMatch, assertNotEquals } from "jsr:@std/assert@1";
import {
  CODE_ALPHABET,
  generateRecoveryCode,
  hashEmail,
  hashRecoveryCode,
  isValidPassword,
  normalizeRecoveryCode,
} from "./recovery.ts";

Deno.test("generated code has 5 groups of 4 from the alphabet", () => {
  for (let i = 0; i < 50; i++) {
    const code = generateRecoveryCode();
    assertMatch(code, /^[A-Z2-9]{4}(-[A-Z2-9]{4}){4}$/);
    for (const ch of code.replaceAll("-", "")) {
      assert(CODE_ALPHABET.includes(ch), `unexpected char ${ch}`);
    }
  }
});

Deno.test("generated codes are distinct", () => {
  const seen = new Set<string>();
  for (let i = 0; i < 200; i++) seen.add(generateRecoveryCode());
  assertEquals(seen.size, 200);
});

Deno.test("normalize accepts dashes, spaces, and lowercase", () => {
  const code = generateRecoveryCode();
  assertEquals(normalizeRecoveryCode(code), code.replaceAll("-", ""));
  assertEquals(
    normalizeRecoveryCode("  abcd efgh jkmn pqrs tuvw "),
    "ABCDEFGHJKMNPQRSTUVW",
  );
});

Deno.test("normalize rejects wrong length and look-alike characters", () => {
  assertEquals(normalizeRecoveryCode(""), null);
  assertEquals(normalizeRecoveryCode("ABCD-EFGH"), null);
  assertEquals(normalizeRecoveryCode("ABCD-EFGH-JKMN-PQRS-TUV0"), null); // 0 not in alphabet
  assertEquals(normalizeRecoveryCode("ABCD-EFGH-JKMN-PQRS-TUVI"), null); // I not in alphabet
});

Deno.test("hash is stable for equivalent inputs and differs for different codes", async () => {
  const code = generateRecoveryCode();
  const a = await hashRecoveryCode(code);
  const b = await hashRecoveryCode(code.toLowerCase().replaceAll("-", " "));
  assertEquals(a, b);
  assertNotEquals(a, await hashRecoveryCode(generateRecoveryCode()));
  assertEquals(a.length, 64);
});

Deno.test("hashRecoveryCode rejects an invalid code", async () => {
  let threw = false;
  try {
    await hashRecoveryCode("NOTVALID");
  } catch {
    threw = true;
  }
  assert(threw);
});

Deno.test("email hash ignores case and surrounding spaces", async () => {
  assertEquals(
    await hashEmail("  Someone@Example.com "),
    await hashEmail("someone@example.com"),
  );
});

Deno.test("password length rule", () => {
  assertEquals(isValidPassword("12345"), false);
  assertEquals(isValidPassword("123456"), true);
  assertEquals(isValidPassword("a".repeat(72)), true);
  assertEquals(isValidPassword("a".repeat(73)), false);
});
