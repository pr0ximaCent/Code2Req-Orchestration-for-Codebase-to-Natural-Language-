// Package historymanager returns additional context from chat history
package historymanager

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"sysmodeler/app/chatbot/agent"
	"sysmodeler/app/chatbot/llm"
	chatbottypes "sysmodeler/app/chatbot/types"
	chatsvc "sysmodeler/app/services/chat"
)

// TODO in prompts.go raises this to a rolling 20 with compaction; until then it stays at what the prompt promises the model.
const historyDepth = 5
const maxTurnChars = 4000

func Run(ctx context.Context, chatContext agent.ChatContext, userMessage string) (string, error) {
	messages, loadErr := chatsvc.LoadHistory(ctx, chatContext.UserID, chatContext.ThreadID, historyDepth+1)
	if loadErr != nil {
		slog.Error("history manager: history load failed",
			"user_id", chatContext.UserID, "thread_id", chatContext.ThreadID,
			"message_id", chatContext.MessageID, "err", loadErr)
		return "", loadErr
	}

	transcript := formatHistory(messages, chatContext.MessageID)
	if transcript == "" {
		// Nothing said before this message: there is no context to recover, and
		// asking the model to find some invites it to invent one.
		slog.Warn("history manager: no usable transcript",
			"thread_id", chatContext.ThreadID, "message_id", chatContext.MessageID,
			"messages", len(messages))
		return "", nil
	}

	filled := strings.NewReplacer(
		"{user_message}", strings.TrimSpace(userMessage),
		"{history}", transcript,
	).Replace(prompt)

	response, runErr := llm.RunWithoutStream(ctx, llm.Inputs{Prompt: filled}, llm.LITE)
	if runErr != nil {
		return "", runErr
	}
	return strings.TrimSpace(response), nil
}

func formatHistory(messages []chatsvc.Message, currentMessageID string) string {
	var out strings.Builder

	for _, message := range messages {
		if currentMessageID != "" && message.MessageID == currentMessageID {
			continue
		}

		userText := strings.TrimSpace(message.UserMessage)
		assistantText := formatBlocks(message.Response)
		if userText == "" && assistantText == "" {
			continue
		}

		if userText != "" {
			fmt.Fprintf(&out, "user: %s\n", truncate(userText))
		}
		if len(message.AttachmentNames) > 0 {
			fmt.Fprintf(&out, "user attachments: %s\n", strings.Join(message.AttachmentNames, ", "))
		}
		if assistantText != "" {
			fmt.Fprintf(&out, "ai: %s\n", truncate(assistantText))
		}
		out.WriteString("\n")
	}

	return strings.TrimSpace(out.String())
}

func formatBlocks(blocks []chatbottypes.StreamBlock) string {
	var lines []string

	for _, block := range blocks {
		switch block.Type {
		case chatbottypes.StreamBlockTypeText:
			if text := strings.TrimSpace(block.Text); text != "" {
				lines = append(lines, text)
			}
		case chatbottypes.StreamBlockTypeTool:
			if detail := block.FileActionDetail; detail != nil {
				lines = append(lines, fmt.Sprintf("[%s %s]", detail.Action, detail.FileName))
			}
		}
	}

	return strings.TrimSpace(strings.Join(lines, "\n"))
}

func truncate(text string) string {
	if len(text) <= maxTurnChars {
		return text
	}
	return text[:maxTurnChars] + " …[truncated]"
}
