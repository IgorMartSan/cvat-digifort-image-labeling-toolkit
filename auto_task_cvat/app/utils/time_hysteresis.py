import time

class TimeHysteresis:
    def __init__(self, activation_time: float, deactivation_time: float):
        self.activation_time = activation_time
        self.deactivation_time = deactivation_time
        self.current_state = False
        self.previous_state = False
        self.last_change_time = time.time()
        self.input_state = False
        self._state_changed = False

    def update(self, state: bool):
        current_time = time.time()

        if state != self.input_state:
            self.last_change_time = current_time
            self.input_state = state

        time_elapsed = current_time - self.last_change_time
        new_state = self.current_state

        if self.current_state:
            if not state and time_elapsed >= self.deactivation_time:
                new_state = False
        else:
            if state and time_elapsed >= self.activation_time:
                new_state = True

        if new_state != self.current_state:
            self.previous_state = self.current_state
            self.current_state = new_state
            self._state_changed = True
            self.last_change_time = current_time
        else:
            self._state_changed = False  # Nenhuma troca

    def has_changed(self) -> bool:
        """Retorna True somente no momento da troca."""
        return self._state_changed

    def get_state(self) -> bool:
        return self.current_state

    def get_previous_state(self) -> bool:
        return self.previous_state
