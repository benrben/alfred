import { Action, ActionPanel, Detail, Icon, List } from "@raycast/api";
import { useEffect, useState } from "react";
import { HistoryItem, readHistory } from "./lib/engine";
import { formatHistoryTitle, formatHistoryWhen } from "./lib/view-logic";

export default function Command() {
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    setItems(readHistory(50));
    setIsLoading(false);
  }, []);

  return (
    <List
      isLoading={isLoading}
      searchBarPlaceholder="Search recent results…"
      isShowingDetail
    >
      {items.length === 0 && !isLoading ? (
        <List.EmptyView
          title="No history yet"
          description="Dictate or transform some text first."
        />
      ) : (
        items.map((item, i) => {
          const when = formatHistoryWhen(item.ts);
          return (
            <List.Item
              key={`${item.ts}-${i}`}
              title={formatHistoryTitle(item.text)}
              accessories={[{ text: `${item.chars}c` }]}
              detail={
                <List.Item.Detail
                  markdown={item.text}
                  metadata={
                    <List.Item.Detail.Metadata>
                      <List.Item.Detail.Metadata.Label
                        title="When"
                        text={when}
                      />
                      <List.Item.Detail.Metadata.Label
                        title="Source"
                        text={item.source ?? "—"}
                      />
                      <List.Item.Detail.Metadata.Label
                        title="Length"
                        text={`${item.chars} chars`}
                      />
                    </List.Item.Detail.Metadata>
                  }
                />
              }
              actions={
                <ActionPanel>
                  <Action.CopyToClipboard title="Copy" content={item.text} />
                  <Action.Paste
                    title="Paste to Frontmost App"
                    content={item.text}
                  />
                  <Action.Push
                    title="Open"
                    icon={Icon.Maximize}
                    target={
                      <Detail
                        markdown={item.text}
                        actions={
                          <ActionPanel>
                            <Action.CopyToClipboard
                              title="Copy"
                              content={item.text}
                            />
                            <Action.Paste
                              title="Paste to Frontmost App"
                              content={item.text}
                            />
                          </ActionPanel>
                        }
                      />
                    }
                  />
                </ActionPanel>
              }
            />
          );
        })
      )}
    </List>
  );
}
