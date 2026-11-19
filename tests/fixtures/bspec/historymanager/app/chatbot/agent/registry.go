package agent

import (
	"context"
	"fmt"
	"sort"
	"strings"
	"sync"
)

// Route is the name the router dispatches on, and the name the model writes
// when it picks an agent.
type Route string

// Request is what every agent is handed. It carries who is asking, what they
// asked, and whatever the router could resolve about the files they meant.
type Request struct {
	ChatContext ChatContext
	Input       UserInput
	// TargetFileIDs are the files the router resolved from the prompt and the
	// workspace, empty when the request is about the project as a whole.
	TargetFileIDs []string
}

// Result is what an agent hands back to the router. Most agents stream their
// own output and return an empty Result; the fields exist for the ones that
// change what the router does next.
type Result struct {
	// Reroute asks the router to run the whole routing pass again — the
	// history manager recovers the context the first pass was missing and then
	// wants the request classified against it.
	Reroute bool
	// History is the recovered context that reroute should carry.
	History string
}

// RunFunc is one agent's entry point.
type RunFunc func(ctx context.Context, request Request) (Result, error)

// Spec is everything the rest of the system needs to know about an agent: how
// to reach it, and how to describe it to the model that chooses between them.
//
// The description lives here, next to the agent, rather than in the router's
// prompt. It used to live in three places — the route constant, the prompt's
// agent list, and the dispatch switch — and adding an agent meant editing all
// three, with a silent misroute waiting for whichever was forgotten.
type Spec struct {
	Route Route
	// Summary is the one line the router prompt shows for this agent.
	Summary string
	// WhenToUse is the rule that tells this agent apart from its neighbours,
	// and matters most where two of them look alike.
	WhenToUse string
	// Examples are user messages that belong to this agent. A couple of real
	// ones teach the boundary better than another sentence of prose.
	Examples []string
	Run      RunFunc
	// SkipFollowUp turns off the suggestion chips the router offers after a
	// run. The history manager sets it: it recovers context and hands the
	// request back to be routed again, so the run it belongs to has not
	// finished and there is nothing yet to suggest.
	SkipFollowUp bool
	// ChangesProject marks an agent whose whole job is to write the project —
	// generating a model, extracting requirements into it. A caller with view
	// access is told so instead of the agent running, since there is nothing
	// of that job it could do for them.
	ChangesProject bool
}

var (
	registryMu sync.RWMutex
	registry   = map[Route]Spec{}
)

// Register adds an agent to the registry. Agents call it from an init
// function, so importing an agent is what makes it routable and nothing else
// has to be kept in step.
func Register(spec Spec) {
	if spec.Route == "" {
		panic("agent: a spec needs a route")
	}
	if spec.Run == nil {
		panic(fmt.Sprintf("agent: %s has no Run", spec.Route))
	}

	registryMu.Lock()
	defer registryMu.Unlock()
	if _, taken := registry[spec.Route]; taken {
		panic(fmt.Sprintf("agent: %s is registered twice", spec.Route))
	}
	registry[spec.Route] = spec
}

// Lookup returns the agent serving a route.
func Lookup(route Route) (Spec, bool) {
	registryMu.RLock()
	defer registryMu.RUnlock()
	spec, ok := registry[route]
	return spec, ok
}

// All returns every registered agent, in route order so the router's prompt is
// stable between runs and diffs of it stay readable.
func All() []Spec {
	registryMu.RLock()
	defer registryMu.RUnlock()

	specs := make([]Spec, 0, len(registry))
	for _, spec := range registry {
		specs = append(specs, spec)
	}
	sort.Slice(specs, func(i, j int) bool { return specs[i].Route < specs[j].Route })
	return specs
}

// Catalogue renders the registered agents for the router's prompt.
func Catalogue() string {
	var sb strings.Builder
	for _, spec := range All() {
		fmt.Fprintf(&sb, "- %q: %s", spec.Route, spec.Summary)
		if spec.WhenToUse != "" {
			fmt.Fprintf(&sb, " %s", spec.WhenToUse)
		}
		sb.WriteString("\n")
		for _, example := range spec.Examples {
			fmt.Fprintf(&sb, "    e.g. %q\n", example)
		}
	}
	return strings.TrimRight(sb.String(), "\n")
}
