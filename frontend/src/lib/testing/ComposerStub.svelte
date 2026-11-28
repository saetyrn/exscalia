<!-- Stand-in for ChatPanel in tests of the surfaces that host it: just the
     composer, which is all a host reaches into. ChatPanel itself imports
     `$app/state`, which vitest cannot resolve. -->
<div class="chat-panel">
	<form><textarea rows="1"></textarea></form>
</div>
