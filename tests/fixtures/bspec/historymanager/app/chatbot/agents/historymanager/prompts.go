package historymanager

const prompt = `
user_message: {user_message}

These are the last 5 messages between the user and ai.
== history starts ==
{history}
== history ends ==

return all the necessary context from the history so that the ai have full picture of what the user actually wants.
`

/* // TODO
next plans:

give last 20 messages


on nth message,
	if n>=20 and n%10==0:
		compact (n-20+1)-(n-10)
		save the compacted summary into (n-10)
		return (n-20+1)-(n-1) messages
	else:
		return (n-20+1)-(n-1) messages

examples:
on 20th message, compact 1-10 and save into 20-10=10
on 25th message, return 4-24
on 30th message, compact 10-20 and save into 30-10=20

*/
