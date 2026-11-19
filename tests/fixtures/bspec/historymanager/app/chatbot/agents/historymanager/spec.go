package historymanager

import (
	"context"

	"sysmodeler/app/chatbot/agent"
)

func init() { agent.Register(spec) }

var spec = agent.Spec{
	Route:   "history-manager",
	Summary: "Recover what earlier messages in this thread said, then route again.",
	WhenToUse: "Use it when the message only makes sense against something said earlier — a pronoun with no antecedent, " +
		"a follow-up to a change the user has not repeated. It answers nothing itself.",
	Examples: []string{
		"do the same for the other subsystem",
		"undo what you just changed",
	},
	// The run is not finished when this agent returns: it hands the request
	// back to be routed against the context it recovered.
	SkipFollowUp: true,
	Run:          run,
}

func run(ctx context.Context, request agent.Request) (agent.Result, error) {
	history, err := Run(ctx, request.ChatContext, request.Input.Prompt)
	return agent.Result{Reroute: true, History: history}, err
}
