// The text of a Feishu interactive card that the Worker sent: the title, then the
// body. A title cut to its limit ends in "…"; the body then holds the first line whole.
export function cardText(card) {
  const title = card.header.title.content;
  const body = card.elements.map((element) => element.text.content).join("\n");
  return title.endsWith("…") ? body : `${title}\n${body}`;
}
