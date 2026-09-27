// TEST-ONLY FIXTURE. NOT PRODUCTION DATA.
//
// Mirrors the structure of the InterCert frontend's
// src/app/constants/faq-data.ts — `export const <NAME>: { [key: string]:
// FaqItem[] }` dictionaries keyed by page slug — so the offline extraction
// utility can be tested without a frontend checkout.
//
// Every question and answer below is invented for testing. This file is
// never read by preston.sources.service_faq, never read by the composition
// root, and must never be treated as a source of knowledge.

export interface FaqItem {
  question: string;
  answer: string;
}

export const FAQ_VALUE: { [key: string]: FaqItem[] } = {
  "fixture-reachable": [
    {
      question: "First fixture question?",
      answer: "First fixture answer."
    },
    {
      question:
        "Second fixture question, authored across two source lines?",
      answer:
        "Second fixture answer, also wrapped the way the real file wraps long strings."
    },
    { question: 'Third fixture question?', answer: 'Third fixture answer.' }
  ],

  // A key with no corresponding live page: dead content no visitor can
  // reach, and the case the reachability filter exists to exclude.
  "fixture-unreachable": [
    {
      question: "Unreachable fixture question?",
      answer: "No live page has this slug, so this pair is never ingested."
    }
  ],

  "fixture-malformed": [
    { question: "", answer: "An answer whose question is empty." },
    { question: "A question whose answer is empty?", answer: "" },
    { question: "A usable fixture question?", answer: "A usable fixture answer." }
  ]
};

// A second dictionary, to prove the extractor reads the one it is asked
// for and never merges the others.
export const FAQ_OTHER: { [key: string]: FaqItem[] } = {
  "fixture-reachable": [
    {
      question: "Wrong-dictionary fixture question?",
      answer: "A reachable slug, but this dictionary is not the one being extracted."
    }
  ]
};
