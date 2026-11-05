import { useShell } from './context';
import { VideoDialog } from './ui';

export function useVideo() {
  const { openDialog } = useShell();
  return (time = 0) => openDialog('Как CoreTeams собирает команду', <VideoDialog time={time} />);
}
