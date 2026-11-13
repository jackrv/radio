import queue


class DropOldestQueue(queue.Queue):
    """Ограниченная очередь с удалением самого старого элемента."""

    def __init__(self, maxsize: int):
        if maxsize <= 0:
            raise ValueError("maxsize должен быть больше нуля")

        super().__init__(maxsize=maxsize)
        self._dropped = 0

    @property
    def dropped(self) -> int:
        """Количество удалённых из-за переполнения элементов."""
        with self.mutex:
            return self._dropped

    def put(self, item, block=True, timeout=None):
        """Добавляет элемент без ожидания свободного места."""
        if timeout is not None and timeout < 0:
            raise ValueError("timeout не может быть отрицательным")

        with self.not_full:
            if self._qsize() >= self.maxsize:
                self._get()
                self._dropped += 1

                # Учитываем удалённый элемент для Queue.join().
                self.unfinished_tasks -= 1

                if self.unfinished_tasks == 0:
                    self.all_tasks_done.notify_all()

            self._put(item)
            self.unfinished_tasks += 1
            self.not_empty.notify()
