// Command panel is a console front panel for the snack machine.
package main

import (
	"bufio"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"strconv"
	"strings"

	"snackbox/config"
	"snackbox/controller"
	"snackbox/money"
	"snackbox/motor"
)

func main() {
	cfgPath := flag.String("config", "config/machine.json", "settings file")
	jamRate := flag.Float64("jam-rate", 0.1, "chance that a motor attempt jams")
	flag.Parse()

	cfg, err := config.Load(*cfgPath)
	if err != nil {
		slog.Error("load config", "err", err)
		os.Exit(1)
	}
	ctrl, err := controller.New(cfg, controller.WithPush(motor.Simulated(*jamRate)))
	if err != nil {
		slog.Error("start controller", "err", err)
		os.Exit(1)
	}
	defer ctrl.Close()

	run(ctrl, os.Stdin, os.Stdout)
}

func run(ctrl *controller.Controller, in io.Reader, out io.Writer) {
	fmt.Fprintln(out, "commands: coin <cents> | pick <slot> | cancel | temp <c> | refill <slot> <n> | list | quit")
	sc := bufio.NewScanner(in)
	for {
		fmt.Fprintf(out, "[%s] > ", ctrl.Display())
		if !sc.Scan() {
			return
		}
		fields := strings.Fields(sc.Text())
		if len(fields) == 0 {
			continue
		}
		if fields[0] == "quit" || fields[0] == "exit" {
			return
		}
		if fields[0] == "list" {
			printSlots(ctrl, out)
			continue
		}
		ev, err := parse(fields)
		if err != nil {
			fmt.Fprintln(out, "?", err)
			continue
		}
		report(out, ctrl.Handle(ev))
	}
}

func parse(f []string) (controller.Event, error) {
	need := func(n int) error {
		if len(f) != n {
			return fmt.Errorf("%s takes %d argument(s)", f[0], n-1)
		}
		return nil
	}
	switch f[0] {
	case "coin":
		if err := need(2); err != nil {
			return nil, err
		}
		n, err := strconv.Atoi(f[1])
		return controller.Coin{Cents: n}, err
	case "pick":
		if err := need(2); err != nil {
			return nil, err
		}
		return controller.Select{Slot: strings.ToUpper(f[1])}, nil
	case "cancel":
		return controller.Cancel{}, need(1)
	case "temp":
		if err := need(2); err != nil {
			return nil, err
		}
		c, err := strconv.ParseFloat(f[1], 64)
		return controller.Temperature{Celsius: c}, err
	case "refill":
		if err := need(3); err != nil {
			return nil, err
		}
		n, err := strconv.Atoi(f[2])
		return controller.Refill{Slot: strings.ToUpper(f[1]), Count: n}, err
	}
	return nil, fmt.Errorf("unknown command %q", f[0])
}

func report(out io.Writer, r controller.Result) {
	msg := r.Outcome.String()
	if r.Slot != "" {
		msg += " (" + r.Slot + ")"
	}
	if r.Returned > 0 {
		msg += ", returned " + money.Format(r.Returned)
	}
	if r.Err != nil {
		msg += ": " + r.Err.Error()
	}
	fmt.Fprintln(out, msg)
}

func printSlots(ctrl *controller.Controller, out io.Writer) {
	for _, it := range ctrl.Slots() {
		fmt.Fprintf(out, "%s %s %s %s\n",
			money.PadRight(it.ID, 4),
			money.PadRight(it.Name, 16),
			money.PadLeft(money.Format(it.Price), 7),
			money.PadLeft(strconv.Itoa(it.Stock), 3))
	}
}
