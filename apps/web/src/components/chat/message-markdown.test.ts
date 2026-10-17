import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { MessageMarkdown } from "./message-markdown";

const drawn = (text: string) => renderToStaticMarkup(createElement(MessageMarkdown, { text }));

describe("how a message's markdown is drawn", () => {
  it("draws emphasis, paragraphs and lists", () => {
    const html = drawn("**Bold** and\n\n- one\n- two");
    expect(html).toContain("<strong>Bold</strong>");
    expect(html).toContain("<li>one</li>");
  });

  it("keeps a single line break", () => {
    expect(drawn("one\ntwo")).toContain("<br/>");
  });

  it("draws a table written in GFM", () => {
    const html = drawn("| a | b |\n| - | - |\n| 1 | 2 |");
    expect(html).toContain("<table");
    expect(html).toContain("<td");
  });

  it("leaves raw HTML as text", () => {
    const html = drawn('<img src="https://tracker.example/pixel.png"> hi');
    expect(html).not.toContain("<img");
    expect(html).toContain("&lt;img");
  });

  it("shows an image as a link instead of loading it", () => {
    const html = drawn("![a chart](https://example.com/chart.png)");
    expect(html).not.toContain("<img");
    expect(html).not.toContain("preload");
    expect(html).toContain('href="https://example.com/chart.png"');
    expect(html).toContain("a chart");
  });

  it("opens a link in a new tab without handing it the opener", () => {
    const html = drawn("[the docs](https://example.com/docs)");
    expect(html).toContain('target="_blank"');
    expect(html).toContain('rel="noopener noreferrer"');
  });
});
