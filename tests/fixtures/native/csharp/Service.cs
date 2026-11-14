using System.Text.Json;
public interface IRepo { void Save(); }
public class SqlRepo : IRepo
{
    public void Save() { }
    private void Unused() { }
}
public class MemRepo : IRepo
{
    public void Save() { }
}
public class Service
{
    private readonly IRepo _repo;
    public Service(IRepo repo) { _repo = repo; }
    public void Run() { _repo.Save(); Make(); }
    public object Make() { return JsonSerializer.Deserialize<Dto>("{}"); }
    internal void Orphan() { }
}
public class Lonely { }
public class Dto { public string Name { get; set; } public static int Counter; }
